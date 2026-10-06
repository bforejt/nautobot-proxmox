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
  - Roles: NFV, Jump Host, Firewall (dcim.device); DefaultGW, DNS
    (ipam.ipaddress)
  - 0U virtual DeviceTypes: VM-Series, C8000v, C9800-CL, Ubuntu Jump Host VM
    (+ SE350/NUC/Nested Lab Node for the server side)
  - Platforms: ubuntu-jumphost, paloalto-panos, cisco-iosxe, proxmox-ve
  - Statuses Staged/Retired (image promotion gate), forge integration
    records, and every standard Secret RECORD (values never) — under the
    provider the job inputs choose (decision #56): secrets_provider
    (text-file | environment-variable), secrets_path_prefix (text-file
    records point at <prefix>/<name>; default /opt/nautobot/secrets = the
    composer layout), secrets_env_prefix (<PREFIX><NAME> variables).
    Create-only: an existing record is never repointed.
  - Custom fields: platform tunables (day0_builder/machine_type/console_user)
    and dcim.device fields (provisioning_state, vmid, sizing, hypervisor
    targets incl. mgmt_bridge, secrets_group, pa_mgmt_mode)
  - Host baseline (decision #55): dcim.interface fields lag_mode /
    lag_xmit_hash (select, seeded with the code's choices) and primary_member (bool);
    the Secret records ad_bind_password / snmp_community plus every Secret a
    config context's host_baseline block names (SNMPv3 passphrases); the
    ConfigContextSchema nfv-host-baseline (kept equal to the code's)
"""

from django.contrib.contenttypes.models import ContentType

from nautobot.apps.jobs import ChoiceVar, Job, StringVar, register_jobs
from nautobot.dcim.models import Device, DeviceType, Manufacturer, Platform
from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import (
    ConfigContext,
    ConfigContextSchema,
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

from ..lib import host_baseline as hb
from ..lib.secret_records import (
    DEFAULT_ENV_PREFIX,
    DEFAULT_PATH_PREFIX,
    DEFAULT_PROVIDER,
    ENVIRONMENT_VARIABLE,
    TEXT_FILE,
    SecretRecordError,
    describe_secret_records,
    normalize_secret_record_inputs,
    secret_record_defaults,
)

PROVISIONING_STATES = [
    "awaiting_install",
    "bm_installed",
    "baseline_done",
    "fabric_done",
    "vms_deployed",
    "handed_off",
]

# The forge admin bearer: record name per the ExternalIntegration convention,
# file name per the composer's ./add-secret.sh (kept apart on purpose — an
# existing stack's file must stay where its record points).
FORGE_ADMIN_TOKEN_SECRET = "answer-service-admin-token"
FORGE_ADMIN_TOKEN_FILE = "answer_service_admin_token"

# Every credential the jobs resolve gets its record pre-created so
# operators only supply VALUES (./add-secret.sh <name> on composer stacks).
STANDARD_SECRET_NAMES = (
    "jumphost_console_password",
    "xcc_username",
    "xcc_password",
    "host_ssh_username",
    "host_ssh_password",
    "proxmox_token_id",
    "proxmox_token_secret",
    # PA-VM day-0 (pa-bootstrap builder): admin password ships as a
    # phash in bootstrap.xml; authcode is optional BYOL; the SCM PIN
    # pair is read only when a device sets pa_mgmt_mode=scm.
    "pa_admin_password",
    "pa_authcode",
    "scm_registration_pin_id",
    "scm_registration_pin_value",
)


class BootstrapNfvSchema(Job):
    class Meta:
        name = "Bootstrap NFV Data Model"
        description = (
            "Idempotently creates the extensibility records the NFV design uses: "
            "the Hosted On relationship, roles, virtual DeviceTypes, platforms, "
            "custom fields and every Secret RECORD (values never) under the "
            "Secrets provider / path prefix / variable-name prefix inputs "
            "(defaults reproduce the composer layout; an existing record is never "
            "touched). Safe to re-run; no-ops when everything exists."
        )
        has_sensitive_variables = False

    # setup.sh --with-nfv-jobs runs this job through the API with
    # {"data": {}}: every input is optional and run() applies the defaults
    # itself when a value arrives empty.
    secrets_provider = ChoiceVar(
        label="Secrets provider",
        required=False,
        default=DEFAULT_PROVIDER,
        choices=(
            (TEXT_FILE, "text-file — one file per secret"),
            (ENVIRONMENT_VARIABLE, "environment-variable — one variable per secret"),
        ),
        description="Provider of the Secret RECORDS this job creates (values never). "
                    "Create-only: an existing record — repointed or not — is never touched.",
    )
    secrets_path_prefix = StringVar(
        label="text-file path prefix",
        required=False,
        default=DEFAULT_PATH_PREFIX,
        description="text-file records point at <prefix>/<name> (the composer mounts "
                    "./secrets there). Must be absolute. Ignored for environment-variable.",
    )
    secrets_env_prefix = StringVar(
        label="environment-variable name prefix",
        required=False,
        default=DEFAULT_ENV_PREFIX,
        description="environment-variable records name <prefix><NAME>, NAME = the secret name "
                    "upper-cased with '-' -> '_' (e.g. NFV_ + xcc_password -> NFV_XCC_PASSWORD). "
                    "Ignored for text-file.",
    )

    def _log_result(self, kind, name, created):
        self.logger.info("%s %r: %s", kind, name, "created" if created else "exists")

    def _secret_defaults(self, name, file_name=None):
        """Create-only defaults for ONE Secret record under the chosen
        provider (jobs/lib/secret_records.py). Every Secret get_or_create
        below passes this as defaults= — an existing record is never updated."""
        provider, path_prefix, env_prefix = self._secret_inputs
        return secret_record_defaults(name, provider, path_prefix, env_prefix, file_name=file_name)

    def _baseline_secret_names(self):
        """The two conventional names always; plus every name a config
        context's host_baseline block references (SNMPv3 passphrases are per
        user: snmpv3_<user>_auth / _priv unless the context names others)."""
        names = [hb.DEFAULT_AD_BIND_SECRET, hb.DEFAULT_SNMP_COMMUNITY_SECRET]
        for context in ConfigContext.objects.all():
            names += hb.referenced_secret_names(context.data)
        return list(dict.fromkeys(names))

    def run(self, secrets_provider=None, secrets_path_prefix=None, secrets_env_prefix=None):
        # Fail closed BEFORE any write: refuse an unknown provider, a relative
        # path prefix or a malformed variable prefix, and a record name the
        # provider cannot carry (a '.' or ' ' from a config context has no
        # environment-variable spelling) — nothing below has run yet.
        try:
            self._secret_inputs = normalize_secret_record_inputs(
                secrets_provider, secrets_path_prefix, secrets_env_prefix
            )
            self._secret_defaults(FORGE_ADMIN_TOKEN_SECRET, file_name=FORGE_ADMIN_TOKEN_FILE)
            for secret_name in (*STANDARD_SECRET_NAMES, *self._baseline_secret_names()):
                self._secret_defaults(secret_name)
        except SecretRecordError as exc:
            raise ValueError(str(exc)) from exc
        self.logger.info("%s", describe_secret_records(*self._secret_inputs))

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

        # DNS servers per prefix (same pattern as DefaultGW): consumers that
        # need resolvers (PA static init-cfg) read the DNS-role IPs inside the
        # mgmt interface's prefix — first = dns-primary, second = dns-secondary.
        role, created = Role.objects.get_or_create(name="DNS")
        role.content_types.add(ipaddress_ct)
        self._log_result("Role", "DNS (ipam.ipaddress)", created)

        # ---- Manufacturers + 0U virtual DeviceTypes ----
        device_types = [
            ("Lenovo", "ThinkSystem SE350", 1),
            # Current-generation edge target (XCC2, AMD EPYC 8004, 2U short
            # depth) — bmc/profiles/thinkedge-se455-v3.yaml. The model string
            # is the profile key: slugified, it must equal the file name.
            ("Lenovo", "ThinkEdge SE455 V3", 2),
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
            name=FORGE_ADMIN_TOKEN_SECRET,
            defaults=self._secret_defaults(FORGE_ADMIN_TOKEN_SECRET, file_name=FORGE_ADMIN_TOKEN_FILE),
        )
        self._log_result("Secret", f"{FORGE_ADMIN_TOKEN_SECRET} (record only)", created)
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

        # ---- Standard operational Secret RECORDS (values never; #44 rule) ----
        # Under the chosen provider/prefix (STANDARD_SECRET_NAMES above).
        # Create-only: a record an admin repointed (e.g. to another
        # provider) is never touched.
        for secret_name in STANDARD_SECRET_NAMES:
            _, created = Secret.objects.get_or_create(
                name=secret_name,
                defaults=self._secret_defaults(secret_name),
            )
            self._log_result("Secret", f"{secret_name} (record only)", created)

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
        for i, builder in enumerate(("native-cloudinit", "pa-bootstrap")):  # extend as builders ship
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
            # machine_type: pin the exact q35 version (e.g. pc-q35-9.0) after
            # the first successful lab boot — PAN-OS maps NICs by PCI-ID with
            # no MAC fallback, so machine-version drift can rewire ethernet1/x.
            "paloalto-panos": {"day0_builder": "pa-bootstrap", "machine_type": "q35"},
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
            # Optional: two-bridge hosts (decision #20 — vmbr0 mgmt / vmbr1
            # data). Position-0 (mgmt) NICs land here when set; empty = every
            # NIC on vm_bridge (single-bridge behavior unchanged).
            ("mgmt_bridge", "text", "Mgmt Bridge"),
            ("vm_storage", "text", "VM Disk Storage"),
            ("import_storage", "text", "Import Storage"),
            # PA-VM management mode (decision #2: per-VM attribute, not a code
            # branch): standalone (default when empty) or scm.
            ("pa_mgmt_mode", "select", "PA Mgmt Mode"),
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
            if key == "pa_mgmt_mode":
                for i, mode in enumerate(("standalone", "scm")):
                    _, ch_created = CustomFieldChoice.objects.get_or_create(
                        custom_field=cf, value=mode, defaults={"weight": (i + 1) * 10}
                    )
                    if ch_created:
                        self._log_result("  choice", mode, True)

        self._host_baseline()

        return "NFV data model bootstrapped (idempotent — safe to re-run)."

    def _host_baseline(self):
        """Decision #55: the SoT model the Host Baseline job reads."""
        # ---- interface custom fields: the bond/bridge model ----
        # Bonds and bridges are native interfaces (type lag/bridge, members via
        # Interface.lag / Interface.bridge, mtu, mode tagged-all); only what
        # Nautobot has no slot for is a custom field. The select choices are
        # seeded with the code's (the day0_builder handshake; never deleted).
        interface_ct = ContentType.objects.get(app_label="dcim", model="interface")
        for key, cf_type, label, choices in (
            (hb.CF_LAG_MODE, "select", "Bond Mode", hb.LAG_MODES),
            (hb.CF_LAG_XMIT_HASH, "select", "Bond Transmit Hash Policy", hb.XMIT_HASH_POLICIES),
            (hb.CF_PRIMARY_MEMBER, "boolean", "Primary Member", ()),
        ):
            cf, created = CustomField.objects.get_or_create(
                key=key, defaults={"type": cf_type, "label": label, "grouping": "NFV"},
            )
            cf.content_types.add(interface_ct)
            self._log_result("CustomField", f"{key} (interface)", created)
            for i, value in enumerate(choices):
                _, ch_created = CustomFieldChoice.objects.get_or_create(
                    custom_field=cf, value=value, defaults={"weight": (i + 1) * 10}
                )
                if ch_created:
                    self._log_result("  choice", value, True)

        # ---- Secret RECORDS the baseline resolves (values never) ----
        # _baseline_secret_names(): the conventional pair plus every name a
        # config context references. Create-only, under the chosen provider.
        for secret_name in self._baseline_secret_names():
            _, created = Secret.objects.get_or_create(
                name=secret_name,
                defaults=self._secret_defaults(secret_name),
            )
            self._log_result("Secret", f"{secret_name} (record only)", created)

        # ---- ConfigContextSchema for host_baseline (shape only) ----
        # Code-owned like the choice lists: brought back to the code's schema
        # on every run. Attach it to the contexts that carry host_baseline for
        # edit-time validation; the job enforces required-ness on the merge.
        schema = ConfigContextSchema.objects.filter(name=hb.CONTEXT_SCHEMA_NAME).first()
        description = (
            "Shape of the NFV host_baseline config-context key (Host Baseline job + firstboot "
            "inputs; nautobot-proxmox decision #55). Maintained by Bootstrap NFV Data Model."
        )
        if schema is None:
            schema = ConfigContextSchema(name=hb.CONTEXT_SCHEMA_NAME, description=description,
                                         data_schema=hb.CONTEXT_JSON_SCHEMA)
            schema.validated_save()
            self._log_result("ConfigContextSchema", hb.CONTEXT_SCHEMA_NAME, True)
        elif schema.data_schema != hb.CONTEXT_JSON_SCHEMA:
            schema.data_schema = hb.CONTEXT_JSON_SCHEMA
            schema.description = description
            schema.validated_save()
            self.logger.info("ConfigContextSchema %r: updated to this release's shape", hb.CONTEXT_SCHEMA_NAME)
        else:
            self._log_result("ConfigContextSchema", hb.CONTEXT_SCHEMA_NAME, False)


register_jobs(BootstrapNfvSchema)
