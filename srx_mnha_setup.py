#!/usr/bin/env python3
"""
Juniper SRX MNHA (Multi-Node High Availability) Setup Script
=============================================================
Configures via SSH:
  - ICL (Inter-Chassis Link) on both nodes
  - LAN interfaces on both nodes
  - MNHA chassis high-availability parameters

Tested against: Junos OS 21.x / 22.x / 23.x on SRX1500, SRX4100, SRX4600
"""

import argparse
import getpass
import os
import pathlib
import paramiko
import time
import sys
import textwrap

# ─── BUILT-IN DEFAULTS (lowest priority) ─────────────────────────────────────
# Priority order: CLI args  >  .env file  >  these defaults

DEFAULTS = {
    # Management IPs (used to SSH into each node)
    "n1_host":       "10.0.0.1",
    "n2_host":       "10.0.0.2",
    # ICL link IPs (/30 point-to-point between the two nodes)
    "n1_icl_ip":     "10.255.255.1/30",
    "n2_icl_ip":     "10.255.255.2/30",
    # LAN IPs (facing downstream network)
    "n1_lan_ip":     "10.10.10.1/24",
    "n2_lan_ip":     "10.10.10.2/24",
    # Interfaces
    "icl_interface": "ge-0/0/0",
    "lan_interface": "ge-0/0/2",
    # SSH
    "username":      "admin",
    "port":          22,
    # MNHA timers
    "hb_interval":   1000,
    "hb_threshold":  3,
}

# ─── .ENV FILE LOADER ─────────────────────────────────────────────────────────


def load_env_file(path: str) -> dict:
    """
    Parse a .env file and return its key=value pairs as a dict.
    Lines starting with # are treated as comments and ignored.
    No external library needed.

    Example .env file:
        N1_HOST=192.168.1.1
        N2_HOST=192.168.1.2
        N1_ICL_IP=169.254.0.1/30
        N2_ICL_IP=169.254.0.2/30
        N1_LAN_IP=10.10.10.1/24
        N2_LAN_IP=10.10.10.2/24
        N1_ICL_IFACE=ge-0/0/0
        N2_ICL_IFACE=ge-0/0/0
        N1_LAN_IFACE=ge-0/0/2
        N2_LAN_IFACE=ge-0/0/2
        N1_USER=admin
        N2_USER=admin
        SSH_PORT=22
        SSH_PASSWORD=Admin@123
        HB_INTERVAL=1000
        HB_THRESHOLD=3
    """
    env: dict = {}
    p = pathlib.Path(path)
    if not p.exists():
        return env
    for lineno, raw in enumerate(p.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"  [.env] Skipping malformed line {lineno}: {raw!r}")
            continue
        key, _, value = line.partition("=")
        # Strip inline comments (e.g. VALUE=10.0.0.1  # some comment → 10.0.0.1)
        value = value.split("#")[0].strip().strip('"').strip("'")
        env[key.strip().lower()] = value
    return env


def merge_config(env: dict) -> dict:
    """
    Merge .env values on top of built-in DEFAULTS.
    .env keys map directly to DEFAULTS keys (lowercased, underscored).
    Also handles the SSH_PASSWORD, SSH_PORT aliases used in the .env.
    """
    merged = dict(DEFAULTS)
    key_map = {
        # .env key         → DEFAULTS key
        "n1_host":         "n1_host",
        "n2_host":         "n2_host",
        "n1_icl_ip":       "n1_icl_ip",
        "n2_icl_ip":       "n2_icl_ip",
        "n1_lan_ip":       "n1_lan_ip",
        "n2_lan_ip":       "n2_lan_ip",
        "n1_icl_iface":    "icl_interface",
        "n2_icl_iface":    "icl_interface",   # shared default
        "n1_lan_iface":    "lan_interface",
        "n2_lan_iface":    "lan_interface",
        "n1_user":         "username",
        "n2_user":         "username",
        "ssh_port":        "port",
        "ssh_password":    "password",
        "hb_interval":     "hb_interval",
        "hb_threshold":    "hb_threshold",
    }
    # Per-node iface overrides stored separately
    if "n1_icl_iface" in env:
        merged["n1_icl_iface"] = env["n1_icl_iface"]
    if "n2_icl_iface" in env:
        merged["n2_icl_iface"] = env["n2_icl_iface"]
    if "n1_lan_iface" in env:
        merged["n1_lan_iface"] = env["n1_lan_iface"]
    if "n2_lan_iface" in env:
        merged["n2_lan_iface"] = env["n2_lan_iface"]
    if "n1_user" in env:
        merged["n1_user"] = env["n1_user"]
    if "n2_user" in env:
        merged["n2_user"] = env["n2_user"]

    for env_key, default_key in key_map.items():
        if env_key in env:
            val = env[env_key]
            # cast numerics
            if default_key in ("port", "hb_interval", "hb_threshold"):
                try:
                    val = int(val)
                except ValueError:
                    pass
            merged[default_key] = val
    return merged


# ─── CLI ARG PARSER ───────────────────────────────────────────────────────────


def parse_args(cfg: dict) -> argparse.Namespace:
    """
    Parse CLI arguments. cfg holds merged defaults + .env values,
    so every argument is optional — omit any to use the default/env value.
    """
    p = argparse.ArgumentParser(
        description="Configure Juniper SRX MNHA pair (ICL + LAN) over SSH",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Values are resolved in this order:\n"
            "  CLI argument  >  .env file  >  built-in defaults\n\n"
            "Example .env file (srx_mnha.env):\n"
            "  N1_HOST=10.0.0.1\n"
            "  N2_HOST=10.0.0.2\n"
            "  N1_ICL_IP=169.254.0.1/30\n"
            "  N2_ICL_IP=169.254.0.2/30\n"
            "  N1_LAN_IP=10.10.10.1/24\n"
            "  N2_LAN_IP=10.10.10.2/24\n"
            "  SSH_PASSWORD=Admin@123\n"
        ),
    )

    p.add_argument("--env-file", default="srx_mnha.env", metavar="FILE",
                   help=".env file to load defaults from")

    # ── Node 1 ───────────────────────────────────────────────────────────────
    g1 = p.add_argument_group("Node 1 (primary)")
    g1.add_argument("--n1-host",      default=cfg.get("n1_host"),      metavar="IP")
    g1.add_argument("--n1-user",      default=cfg.get("n1_user",
                                       cfg["username"]),                metavar="USER")
    g1.add_argument("--n1-icl-ip",    default=cfg.get("n1_icl_ip"),    metavar="A.B.C.D/30")
    g1.add_argument("--n1-lan-ip",    default=cfg.get("n1_lan_ip"),    metavar="A.B.C.D/24")
    g1.add_argument("--n1-icl-iface", default=cfg.get("n1_icl_iface",
                                       cfg["icl_interface"]),           metavar="IFACE")
    g1.add_argument("--n1-lan-iface", default=cfg.get("n1_lan_iface",
                                       cfg["lan_interface"]),           metavar="IFACE")

    # ── Node 2 ───────────────────────────────────────────────────────────────
    g2 = p.add_argument_group("Node 2 (secondary)")
    g2.add_argument("--n2-host",      default=cfg.get("n2_host"),      metavar="IP")
    g2.add_argument("--n2-user",      default=cfg.get("n2_user",
                                       cfg["username"]),                metavar="USER")
    g2.add_argument("--n2-icl-ip",    default=cfg.get("n2_icl_ip"),    metavar="A.B.C.D/30")
    g2.add_argument("--n2-lan-ip",    default=cfg.get("n2_lan_ip"),    metavar="A.B.C.D/24")
    g2.add_argument("--n2-icl-iface", default=cfg.get("n2_icl_iface",
                                       cfg["icl_interface"]),           metavar="IFACE")
    g2.add_argument("--n2-lan-iface", default=cfg.get("n2_lan_iface",
                                       cfg["lan_interface"]),           metavar="IFACE")

    # ── Shared ────────────────────────────────────────────────────────────────
    gs = p.add_argument_group("Shared options")
    gs.add_argument("--port",     type=int, default=cfg["port"])
    gs.add_argument("--password", default=cfg.get("password"),
                    help="SSH password. Omit to be prompted securely.")
    gs.add_argument("--verify",   action="store_true",
                    help="Run 'show chassis high-availability' after configuring")

    return p.parse_args()


def build_node_dicts(args: argparse.Namespace) -> tuple[dict, dict]:
    """Build NODE_1 and NODE_2 dicts from resolved config."""

    # Prompt for password securely if not in args or .env
    password = args.password
    if not password:
        password = getpass.getpass("SSH password (used for both nodes): ")

    node1 = {
        "host":            args.n1_host,
        "username":        args.n1_user,
        "password":        password,
        "port":            args.port,
        "local_id":        1,
        "peer_id":         2,
        "icl_interface":   args.n1_icl_iface,
        "icl_ip":          args.n1_icl_ip,
        "lan_interface":   args.n1_lan_iface,
        "lan_ip":          args.n1_lan_ip,
        "lan_description": "LAN-NODE1",
    }
    node2 = {
        "host":            args.n2_host,
        "username":        args.n2_user,
        "password":        password,
        "port":            args.port,
        "local_id":        2,
        "peer_id":         1,
        "icl_interface":   args.n2_icl_iface,
        "icl_ip":          args.n2_icl_ip,
        "lan_interface":   args.n2_lan_iface,
        "lan_ip":          args.n2_lan_ip,
        "lan_description": "LAN-NODE2",
    }
    return node1, node2



# ─── SSH HELPERS ─────────────────────────────────────────────────────────────


def ssh_connect(device: dict) -> paramiko.SSHClient:
    """Open an SSH connection to a Juniper SRX device."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"  [SSH] Connecting to {device['host']}:{device['port']} "
          f"as {device['username']} ...")
    client.connect(
        hostname=device["host"],
        port=device["port"],
        username=device["username"],
        password=device["password"],
        look_for_keys=False,
        allow_agent=False,
        timeout=15,
    )
    print(f"  [SSH] Connected to {device['host']}")
    return client


def wait_for_prompt(channel: paramiko.Channel,
                    expected: str,
                    timeout: float = 10.0,
                    poll: float = 0.2) -> str:
    """
    Read from the channel until `expected` appears in the output
    or `timeout` seconds have elapsed.
    Returns the accumulated output so far.
    """
    output = ""
    elapsed = 0.0
    while elapsed < timeout:
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            if expected in output:
                return output
        time.sleep(poll)
        elapsed += poll
    return output  # return whatever arrived even if prompt not seen


def run_junos_config(device: dict, config_commands: list[str]) -> str:
    """
    Connect to a Junos device and walk through each mode transition
    by waiting for the exact prompt before moving on:

      Login shell  →  [%]  or  direct Junos CLI  [>]
        cli                  →  operational prompt  [>]
        configure            →  CLI edit mode       [#]  ← [edit] prompt
        save /var/tmp/...    →  checkpoint saved    [#]  ← pre-change snapshot
        set ...              →  [#]  (one per command)
        commit check         →  configuration check succeeds  ← dry-run first
        commit               →  commit complete               ← activate only if check passes
        exit configuration   →  [>]
        exit cli             →  shell / disconnect
    """
    full_output = ""
    client = ssh_connect(device)
    # Set wide terminal (width=512) so Junos never wraps or truncates
    # long command echoes with "..." — the full command is always visible.
    channel = client.invoke_shell(term="vt100", width=512, height=64)

    # ── Step 1: drain login banner and detect initial prompt ─────────────────
    print("    [1] Waiting for initial prompt ...")
    banner = wait_for_prompt(channel, expected=">", timeout=10)
    full_output += banner

    # ── Step 2: ensure we are in Junos CLI (not Unix shell) ──────────────────
    # Junos operational prompt ends with '>'  e.g. root@srx1>
    # Unix shell prompt ends with '%' or '$'  e.g. root@srx1%
    # We always send 'cli' — harmless if already in Junos CLI.
    print("    [2] Entering Junos CLI ...")
    channel.send("cli\n")
    out = wait_for_prompt(channel, expected=">", timeout=10)
    full_output += out
    if ">" in out:
        print("        Junos CLI operational prompt (>) confirmed")
    else:
        print("        [WARNING] Could not confirm Junos CLI prompt — output:")
        print(textwrap.indent(out, "          "))

    # ── Step 3: enter config mode and confirm [edit] ──────────────────────────
    # IMPORTANT: wait for "[edit]" not just "#" — the Unix shell also has "#"
    # in its prompt (e.g. root@host:~#) which would be a false match.
    # Junos config mode always prints "[edit]" on its own line above the prompt.
    print("    [3] Entering CLI edit mode (configure) ...")
    channel.send("configure\n")
    out = wait_for_prompt(channel, expected="[edit]", timeout=10)
    full_output += out
    if "[edit]" in out:
        print("        Config mode confirmed — [edit] prompt detected ✓")
    else:
        print("        [ERROR] Config mode NOT entered. Raw output:")
        print(textwrap.indent(out, "          "))
        print("        Aborting — device may still be in Unix shell.")
        channel.close()
        client.close()
        raise RuntimeError(f"Failed to enter Junos config mode on {device['host']}")

    # ── Step 4: save checkpoint before touching anything ─────────────────────
    checkpoint_file = "/var/tmp/pre_mnha_checkpoint.conf"
    print(f"    [4] Saving checkpoint → {checkpoint_file} ...")
    channel.send(f"save {checkpoint_file}\n")
    out = wait_for_prompt(channel, expected="[edit]", timeout=10)
    full_output += out
    if "saved" in out.lower() or checkpoint_file in out:
        print(f"        Checkpoint saved ✓  ({checkpoint_file})")
    else:
        print(f"        [WARNING] Checkpoint save response unclear — proceeding anyway")
    print(f"        To restore later, run on device:")
    print(f"          load override {checkpoint_file}")
    print(f"          commit")

    # ── Step 5: push each 'set' command individually ─────────────────────────
    print(f"    [5] Pushing {len(config_commands)} configuration commands ...")
    errors_found = False
    for cmd in config_commands:
        channel.send(cmd + "\n")
        out = wait_for_prompt(channel, expected="[edit]", timeout=5)
        full_output += out
        # Surface any error lines immediately
        for line in out.splitlines():
            if any(kw in line.lower() for kw in ("error", "invalid", "unknown")):
                print(f"        [!] {line.strip()}")
                errors_found = True

    # ── Step 5a: skip commit entirely if set commands had errors ─────────────
    if errors_found:
        print("\n    [6] SKIPPING commit — errors detected in set commands above.")
        print("        Running 'rollback 0' to discard candidate config ...")
        channel.send("rollback 0\n")
        wait_for_prompt(channel, expected="[edit]", timeout=5)
        print(f"        Restore from checkpoint manually if needed:")
        print(f"          load override {checkpoint_file}")
        print(f"          commit")

    else:
        # ── Step 5b: commit check (dry-run validation) ────────────────────────
        # "commit check" validates syntax and semantics WITHOUT activating config.
        # Only proceed to "commit" if check passes.
        print("    [6] Running commit check (dry-run validation) ...")
        channel.send("commit check\n")
        out = wait_for_prompt(channel, expected="[edit]", timeout=20)
        full_output += out

        check_ok = "configuration check succeeds" in out.lower()
        check_err = "error" in out.lower()

        if check_err or not check_ok:
            print("        [ERROR] Commit check failed — full output below:")
            print("        " + "─" * 50)
            for line in out.splitlines():
                if line.strip():
                    print(f"          {line}")
            print("        " + "─" * 50)
            print("        Running 'rollback 0' to discard candidate config ...")
            channel.send("rollback 0\n")
            wait_for_prompt(channel, expected="[edit]", timeout=5)
            print(f"        Restore from checkpoint if needed:")
            print(f"          load override {checkpoint_file}")
            print(f"          commit")

        else:
            print("        Commit check passed ✓ — configuration is valid")

            # ── Step 5c: commit (activate configuration) ──────────────────────
            print("    [7] Committing configuration ...")
            channel.send("commit\n")
            # Junos may take up to 30s on large configs
            out = wait_for_prompt(channel, expected="commit complete", timeout=30)
            full_output += out

            if "commit complete" in out.lower():
                print("        Commit complete ✓  — configuration is now live")
                print(f"        Checkpoint saved at : {checkpoint_file}")
                print(f"        To rollback later   : load override {checkpoint_file} → commit")

            elif "error" in out.lower():
                print("        [ERROR] Commit failed — rolling back to checkpoint ...")
                for line in out.splitlines():
                    if "error" in line.lower():
                        print(f"          {line.strip()}")
                channel.send(f"load override {checkpoint_file}\n")
                wait_for_prompt(channel, expected="[edit]", timeout=15)
                channel.send("commit\n")
                out2 = wait_for_prompt(channel, expected="commit complete", timeout=30)
                full_output += out2
                if "commit complete" in out2.lower():
                    print("        Rolled back to pre-change checkpoint ✓")
                else:
                    print("        [WARNING] Checkpoint rollback unclear — check device manually.")

            else:
                # Drain remaining output and try once more
                out2 = wait_for_prompt(channel, expected="[edit]", timeout=15)
                full_output += out2
                if "commit complete" in (out + out2).lower():
                    print("        Commit complete ✓")
                else:
                    print("        [WARNING] Commit status unclear — verify on device with:")
                    print("          show system commit")

    # ── Step 6: exit configuration mode, then CLI ────────────────────────────
    print("    [7] Exiting configuration mode ...")
    channel.send("exit\n")
    wait_for_prompt(channel, expected=">", timeout=5)

    channel.send("exit\n")
    time.sleep(1)

    channel.close()
    client.close()
    return full_output


# ─── COMMAND BUILDERS ─────────────────────────────────────────────────────────


def build_icl_commands(node: dict) -> list[str]:
    """
    JunOS 'set' commands to configure the ICL (Inter-Chassis Link) interface.

    Required MNHA commands (minimum needed):
      - local-id      : identifies this node (1 or 2)
      - peer-id       : identifies the other node
      - icl-interface : the physical port wired directly between the two SRX devices

    Optional (Junos already has sensible defaults — only uncomment to override):
      - heartbeat-interval   default 1000 ms
      - heartbeat-threshold  default 3 missed heartbeats
      - session-synchronization  (recommended for stateful failover)
      - traceoptions             (for troubleshooting only)
    """
    iface    = node["icl_interface"]
    icl_ip   = node["icl_ip"]
    peer_id  = node["peer_id"]
    # peer_icl_ip: the ICL IP of the OTHER node (no prefix) — used as peer-ip
    peer_ip  = node["peer_icl_ip"]

    return [
        # ── 0. Clean slate — delete existing chassis HA config first ─────────
        # Prevents "local-id must be different from peer-id" conflicts from
        # leftover peer-id values committed by a previous partial run.
        # Only removes the chassis high-availability stanza — all interface
        # IPs, zones, and routes are untouched.
        "delete chassis high-availability",

        # ── ICL physical interface ────────────────────────────────────────────
        f"set interfaces {iface} description \"MNHA-ICL-to-peer\"",
        f"set interfaces {iface} unit 0 family inet address {icl_ip}",

        # ── MNHA chassis high-availability — required parameters ──────────────
        # local-id: identifies this node in the HA pair
        f"set chassis high-availability local-id {node['local_id']}",

        # peer-id block: mandatory sub-statements revealed by commit check:
        #   peer-ip          → ICL IP of the peer node (reachable over ICL link)
        #   interface        → local ICL interface used to reach the peer
        #   liveness-detection → BFD settings for peer health monitoring
        f"set chassis high-availability peer-id {peer_id} peer-ip {peer_ip}",
        f"set chassis high-availability peer-id {peer_id} interface {iface}",
        f"set chassis high-availability peer-id {peer_id} liveness-detection minimum-interval 1000",
        f"set chassis high-availability peer-id {peer_id} liveness-detection multiplier 3",
    ]


def build_lan_commands(node: dict) -> list[str]:
    """
    JunOS 'set' commands to configure the LAN-facing interface.

    Each node gets its own unique IP on the same /24 subnet:
      Node 1 → 10.10.10.1/24
      Node 2 → 10.10.10.2/24

    VRRP (optional) adds a shared virtual IP (e.g. 10.10.10.254) so
    LAN clients always have a single gateway regardless of which node
    is active. Uncomment the VRRP block below to enable it.
    """
    iface  = node["lan_interface"]
    cmds = [
        # Physical interface description
        f"set interfaces {iface} description \"{node['lan_description']}\"",
        # unit 0 — logical unit carrying the node's own IP
        f"set interfaces {iface} unit 0 family inet address {node['lan_ip']}",
        # Security zone — bind unit 0 to the trust zone
        f"set security zones security-zone trust interfaces {iface}.0",
        # Allow host-inbound services on the LAN interface
        f"set security zones security-zone trust interfaces {iface}.0 "
        f"host-inbound-traffic system-services ping",
        f"set security zones security-zone trust interfaces {iface}.0 "
        f"host-inbound-traffic system-services ssh",

        # ── OPTIONAL: VRRP — shared virtual gateway for LAN clients ──────────
        # Uncomment the lines below to enable VRRP on the LAN interface.
        # Both nodes share the VIP 10.10.10.254; node with higher priority
        # becomes master. Adjust vrid, priority, and virtual-address as needed.
        #
        # f"set interfaces {iface} unit 0 family inet vrrp-group 10 "
        # f"virtual-address {node.get('lan_vip', '10.10.10.254')}",
        # f"set interfaces {iface} unit 0 family inet vrrp-group 10 "
        # f"priority {node.get('vrrp_priority', 100)}",
        # f"set interfaces {iface} unit 0 family inet vrrp-group 10 "
        # f"preempt",
        # f"set interfaces {iface} unit 0 family inet vrrp-group 10 "
        # f"accept-data",
        # ─────────────────────────────────────────────────────────────────────
    ]
    return cmds


# ─── MAIN ─────────────────────────────────────────────────────────────────────


def configure_node(node: dict, node_label: str) -> None:
    """Configure ICL + LAN on a single SRX node."""
    print(f"\n{'=' * 60}")
    print(f"  Configuring {node_label}  ({node['host']})")
    print(f"{'=' * 60}")

    icl_cmds = build_icl_commands(node)
    lan_cmds = build_lan_commands(node)
    all_cmds  = icl_cmds + lan_cmds

    print(f"\n  Commands to be applied on {node_label}:")
    for cmd in all_cmds:
        print(f"    {cmd}")

    confirm = input(f"\n  Apply to {node_label} ({node['host']})? [y/N]: ").strip().lower()
    if confirm != "y":
        print(f"  Skipped {node_label}.")
        return

    print(f"\n  Applying configuration to {node_label} ...")
    try:
        output = run_junos_config(node, all_cmds)
        print(f"\n  --- Output from {node_label} ---")
        print(textwrap.indent(output, "  "))

        # Check for real errors — look for "syntax error" or "error:" patterns
        # (avoid false positives from words like "icl-interface" containing "error")
        error_lines = [
            line for line in output.splitlines()
            if "syntax error" in line.lower()
            or line.strip().lower().startswith("error:")
            or "invalid input" in line.lower()
        ]
        if error_lines:
            print(f"\n  [WARNING] Errors detected in {node_label} output:")
            for line in error_lines:
                print(f"    {line.strip()}")
        else:
            print(f"\n  [OK] Configuration committed on {node_label}.")

    except Exception as exc:
        print(f"\n  [ERROR] Failed to configure {node_label}: {exc}", file=sys.stderr)
        raise


def verify_connectivity(node: dict, node_label: str) -> None:
    """Quick check — run 'show chassis high-availability' to verify MNHA state."""
    print(f"\n  Verifying MNHA state on {node_label} ({node['host']}) ...")
    try:
        client = ssh_connect(node)
        _, stdout, stderr = client.exec_command(
            "cli -c 'show chassis high-availability'", timeout=10
        )
        out = stdout.read().decode()
        err = stderr.read().decode()
        client.close()
        print(f"\n  --- show chassis high-availability ({node_label}) ---")
        print(textwrap.indent(out or err or "(no output)", "  "))
    except Exception as exc:
        print(f"  [ERROR] Verification failed: {exc}", file=sys.stderr)


def main() -> None:
    # ── Step 0: load .env file first (before argparse so defaults are set) ───
    # Peek at sys.argv for --env-file before full parse
    env_file = "srx_mnha.env"
    for i, arg in enumerate(sys.argv):
        if arg == "--env-file" and i + 1 < len(sys.argv):
            env_file = sys.argv[i + 1]

    env_data = load_env_file(env_file)
    if env_data:
        print(f"  [.env] Loaded {len(env_data)} settings from '{env_file}'")
    else:
        print(f"  [.env] '{env_file}' not found — using built-in defaults")

    # Merge .env on top of built-in defaults, then parse CLI args on top of that
    cfg  = merge_config(env_data)
    args = parse_args(cfg)

    # Build node dicts (password prompted here if not passed via --password / .env)
    NODE_1, NODE_2 = build_node_dicts(args)

    # Set peer_icl_ip: each node's peer-ip = the OTHER node's ICL IP (strip /prefix)
    NODE_1["peer_icl_ip"] = NODE_2["icl_ip"].split("/")[0]
    NODE_2["peer_icl_ip"] = NODE_1["icl_ip"].split("/")[0]

    print("""
╔══════════════════════════════════════════════════════════╗
║    Juniper SRX MNHA Setup — ICL + LAN Configuration     ║
╚══════════════════════════════════════════════════════════╝

 Node 1  :  {} (local-id {})
 Node 2  :  {} (local-id {})

 ICL node1  : {} → {}
 ICL node2  : {} → {}
 LAN node1  : {} → {}
 LAN node2  : {} → {}
""".format(
        NODE_1["host"], NODE_1["local_id"],
        NODE_2["host"], NODE_2["local_id"],
        NODE_1["icl_interface"], NODE_1["icl_ip"],
        NODE_2["icl_interface"], NODE_2["icl_ip"],
        NODE_1["lan_interface"], NODE_1["lan_ip"],
        NODE_2["lan_interface"], NODE_2["lan_ip"],
    ))

    # Configure both nodes
    configure_node(NODE_1, "Node-1 (primary)")
    configure_node(NODE_2, "Node-2 (secondary)")

    # Optional post-config verification
    if args.verify or input(
        "\nRun 'show chassis high-availability' on both nodes? [y/N]: "
    ).strip().lower() == "y":
        verify_connectivity(NODE_1, "Node-1")
        verify_connectivity(NODE_2, "Node-2")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
