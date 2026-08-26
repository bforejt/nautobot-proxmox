"""
Nautobot Job: bootstrap the NFV data-model prerequisites (idempotent).

Everything the NFV design uses is Nautobot extensibility DATA — Relationships,
custom fields, roles, manufacturers, device types, platforms — not schema, so
no App/migrations are needed. This job get_or_creates all of it: run once per
Nautobot instance (dev, prod), safely re-run any time; it reports created vs
already-present per object.

Created here (decision log #8, Device-only modeling):
  - Relationship "Hosted On": hypervisor Device (source, one) -> VNF Devices
    (destination, many)
  - Roles: Hypervisor, Jump Host (Firewall is assumed to exist / is ensured)
  - 0U virtual DeviceTypes: VM-Series, C8000v, C9800-CL, Ubuntu Jump Host VM
    (+ SE350 for the hypervisors themselves)
  - Platforms: paloalto-panos, cisco-iosxe (ubuntu-jumphost ensured)
  - Custom fields on dcim.device: provisioning_state (select w/ lifecycle
    choices), vmid (integer), vcpus/memory_mb/disk_gb overrides (integers)
"""

from django.contrib.contenttypes.models import ContentType

from nautobot.apps.jobs import Job, register_jobs
from nautobot.dcim.models import Device, DeviceType, Manufacturer, Platform
from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import (
    CustomField,
    CustomFieldChoice,
    ExternalIntegration,
    Relationship,
    Role,
    Secret,
    SecretsGroup,
    SecretsGroupAssociation,
    Status,
)

PROVISIONING_STATES = [
    "awaiting_install",
    "bm_installed",
    "baseline_done",
    "fabric_done",
    "vms_deployed",
    "handed_off",
]


class BootstrapNfvSchema(Job):
    class Meta:
        name = "Bootstrap NFV Data Model"
        description = (
            "Idempotently creates the extensibility records the NFV design uses: "
            "the Hosted On relationship, roles, virtual DeviceTypes, platforms, "
            "and custom fields. Safe to re-run; no-ops when everything exists."
        )
        has_sensitive_variables = False

    def _log_result(self, kind, name, created):
        self.logger.info("%s %r: %s", kind, name, "created" if created else "exists")

    def run(self):
        device_ct = ContentType.objects.get(app_label="dcim", model="device")

        # ---- Relationship: Hosted On ----
        rel, created = Relationship.objects.get_or_create(
            key="hosted_on",
            defaults={
                "label": "Hosted On",
                "type": "one-to-many",
                "source_type": device_ct,
                "destination_type": device_ct,
                "source_label": "Hosted VNFs",       # shown on the hypervisor's page
                "destination_label": "Hosted On",    # shown on the VNF's page
            },
        )
        self._log_result("Relationship", "Hosted On (hosted_on)", created)

        # ---- Roles ----
        # "NFV" = the team's role for the servers (their convention:
        # "Hypervisor" is not specific enough and can mean other things).
        for role_name in ("NFV", "Jump Host", "Firewall"):
            role, created = Role.objects.get_or_create(name=role_name)
            role.content_types.add(device_ct)
            self._log_result("Role", role_name, created)

        # Default-gateway marker per prefix (sot-data-contract.md §3): exactly
        # one DefaultGW-role IPAddress per prefix; FHRP addresses keep their
        # VRRP/HSRP/VIP roles. Named DefaultGW because other gateways coexist.
        ipaddress_ct = ContentType.objects.get(app_label="ipam", model="ipaddress")
        role, created = Role.objects.get_or_create(name="DefaultGW")
        role.content_types.add(ipaddress_ct)
        self._log_result("Role", "DefaultGW (ipam.ipaddress)", created)

        # ---- Manufacturers + 0U virtual DeviceTypes ----
        device_types = [
            ("Lenovo", "ThinkSystem SE350", 1),
            ("Palo Alto Networks", "VM-Series", 0),
            ("Cisco Systems", "C8000v", 0),
            ("Cisco Systems", "C9800-CL", 0),
            ("Canonical", "Ubuntu Jump Host VM", 0),
            # L0 lab kit: a VM on a lab Proxmox host standing in for a blank
            # physical server (bmc/profiles/nested-lab-node.yaml).
            ("Proxmox", "Nested Lab Node", 0),
            # Real-hardware PXE test target / small lab hypervisor
            # (bmc/profiles/nuc.yaml).
            ("Intel", "NUC", 1),
        ]
        for mfr_name, model, u_height in device_types:
            mfr, m_created = Manufacturer.objects.get_or_create(name=mfr_name)
            if m_created:
                self._log_result("Manufacturer", mfr_name, True)
            dt, created = DeviceType.objects.get_or_create(
                manufacturer=mfr, model=model, defaults={"u_height": u_height}
            )
            self._log_result("DeviceType", f"{mfr_name} {model}", created)

        # ---- Platforms ----
        # proxmox-ve: the hypervisors' own OS — installer images register as
        # SoftwareVersions under it (same Staged->Active gate as guest images).
        for platform_name in ("ubuntu-jumphost", "paloalto-panos", "cisco-iosxe", "proxmox-ve"):
            _, created = Platform.objects.get_or_create(name=platform_name)
            self._log_result("Platform", platform_name, created)

        # ---- Statuses for the image promotion gate ----
        # Stock Nautobot doesn't scope "Staged" to the software models, and has
        # no "Retired" — but the image lifecycle (Staged -> Active -> Retired)
        # depends on both. Without this, registering a Staged SoftwareVersion
        # fails in a fresh environment.
        sv_ct = ContentType.objects.get(app_label="dcim", model="softwareversion")
        sif_ct = ContentType.objects.get(app_label="dcim", model="softwareimagefile")
        staged, created = Status.objects.get_or_create(name="Staged", defaults={"color": "2196f3"})
        staged.content_types.add(sv_ct, sif_ct)
        self._log_result("Status", "Staged (+softwareversion/imagefile)", created)
        retired, created = Status.objects.get_or_create(name="Retired", defaults={"color": "9e9e9e"})
        retired.content_types.add(sv_ct)
        self._log_result("Status", "Retired (softwareversion)", created)

        # ---- Media forge plumbing (decision #44): records, never values ----
        # The PrepareInstallerMedia job resolves the answer service through
        # the ExternalIntegration below. Bootstrap creates the resolvable
        # SKELETON only — the admin bearer VALUE stays an operational secret
        # (write it: ./add-secret.sh answer_service_admin_token). remote_url
        # seeds the compose-network address (valid on composer AND nfv-helper
        # stacks); CREATE-ONLY — an admin's corrected URL is never touched.
        forge_secret, created = Secret.objects.get_or_create(
            name="answer-service-admin-token",
            defaults={
                "provider": "text-file",
                "parameters": {"path": "/opt/nautobot/secrets/answer_service_admin_token"},
            },
        )
        self._log_result("Secret", "answer-service-admin-token (record only)", created)
        forge_group, created = SecretsGroup.objects.get_or_create(name="nfv-answer-service-admin")
        self._log_result("SecretsGroup", "nfv-answer-service-admin", created)
        # Keyed on the slot (group + access/secret type): if an admin already
        # bound a different secret there, leave their choice alone.
        _, created = SecretsGroupAssociation.objects.get_or_create(
            secrets_group=forge_group,
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_TOKEN,
            defaults={"secret": forge_secret},
        )
        self._log_result("  association", "Generic/token", created)
        _, created = ExternalIntegration.objects.get_or_create(
            name="nfv-answer-service",
            defaults={
                "remote_url": "https://answer-service:8800",
                "verify_ssl": False,
                "secrets_group": forge_group,
            },
        )
        self._log_result("ExternalIntegration", "nfv-answer-service", created)

        # ---- Platform tunables (desired state: in the SoT, stored once) ----
        # Immutable platform FACTS (guest NIC-name order, cloud-init class)
        # live in code. TUNABLES live here as Platform custom fields
        # (sot-data-contract.md). day0_builder is select-typed: this job
        # maintains its choice list to exactly match the builders the job
        # code ships — the code<->data contract handshake; an admin cannot
        # select a builder that does not exist.
        platform_ct = ContentType.objects.get(app_label="dcim", model="platform")
        cf_day0, created = CustomField.objects.get_or_create(
            key="day0_builder",
            defaults={"type": "select", "label": "Day-0 Builder", "grouping": "NFV"},
        )
        cf_day0.content_types.add(platform_ct)
        self._log_result("CustomField", "day0_builder (platform)", created)
        for i, builder in enumerate(("native-cloudinit",)):  # extend as builders ship
            _, ch_created = CustomFieldChoice.objects.get_or_create(
                custom_field=cf_day0, value=builder, defaults={"weight": (i + 1) * 10}
            )
            if ch_created:
                self._log_result("  builder choice", builder, True)
        cf_machine, created = CustomField.objects.get_or_create(
            key="machine_type",
            defaults={"type": "text", "label": "Machine Type", "grouping": "NFV"},
        )
        cf_machine.content_types.add(platform_ct)
        self._log_result("CustomField", "machine_type (platform)", created)

        # Console login username for cloud-init platforms. Verified: Proxmox
        # ciuser overrides only the NAME while cloud-init still applies the
        # template's baked default_user groups/sudo — so this is a genuine
        # deploy-time value (no template rebuild to change it). The fleet
        # console PASSWORD is a Nautobot Secret (jumphost_console_password).
        cf_user, created = CustomField.objects.get_or_create(
            key="console_user",
            defaults={"type": "text", "label": "Console User", "grouping": "NFV"},
        )
        cf_user.content_types.add(platform_ct)
        self._log_result("CustomField", "console_user (platform)", created)

        # Seed platform values — CREATE-ONLY: an admin's adjusted value is
        # never overwritten by a re-run.
        platform_seeds = {
            "ubuntu-jumphost": {"day0_builder": "native-cloudinit", "machine_type": "q35", "console_user": "manager"},
            "paloalto-panos": {"machine_type": "q35"},
            "cisco-iosxe": {"machine_type": "q35"},
        }
        for plat_name, values in platform_seeds.items():
            plat = Platform.objects.filter(name=plat_name).first()
            if plat is None:
                continue
            changed = False
            for key, value in values.items():
                if plat._custom_field_data.get(key) in (None, ""):
                    plat._custom_field_data[key] = value
                    changed = True
                    self._log_result(f"Platform {plat_name}", f"{key}={value}", True)
            if changed:
                plat.validated_save()

        # ---- Custom fields on dcim.device ----
        cf_defs = [
            ("provisioning_state", "select", "Provisioning State"),
            ("vmid", "integer", "Proxmox VMID"),
            ("vcpus", "integer", "vCPUs"),
            ("memory_mb", "integer", "Memory MB"),
            ("disk_gb", "integer", "Disk GB"),
            # Hypervisor-side deployment targets (set by the layout engine):
            ("vm_bridge", "text", "VM Bridge"),
            ("vm_storage", "text", "VM Disk Storage"),
            ("import_storage", "text", "Import Storage"),
            # Per-hypervisor Proxmox API credentials: names a SecretsGroup
            # (Generic/Username = token id, Generic/Secret = token UUID). Empty
            # = fall back to the global proxmox_token_id/secret pair (single-host
            # quickstart). Each standalone node in a pair needs its own token.
            ("secrets_group", "text", "Proxmox SecretsGroup"),
        ]
        for key, cf_type, label in cf_defs:
            cf, created = CustomField.objects.get_or_create(
                key=key,
                defaults={"type": cf_type, "label": label, "grouping": "NFV"},
            )
            cf.content_types.add(device_ct)
            self._log_result("CustomField", key, created)
            if key == "provisioning_state":
                for i, state in enumerate(PROVISIONING_STATES):
                    _, ch_created = CustomFieldChoice.objects.get_or_create(
                        custom_field=cf, value=state, defaults={"weight": (i + 1) * 10}
                    )
                    if ch_created:
                        self._log_result("  choice", state, True)

        return "NFV data model bootstrapped (idempotent — safe to re-run)."


register_jobs(BootstrapNfvSchema)
