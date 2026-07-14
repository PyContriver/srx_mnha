#!/usr/bin/env python3
"""
Juniper SRX MNHA — WAN Interface Setup Script
==============================================
Configures via SSH on both nodes:
  - WAN interface IP address
  - Untrust security zone assignment
  - Default static route (0.0.0.0/0 → WAN gateway)
  - Optional: source NAT for outbound internet access

Reads all settings from the same srx_mnha.env file used by srx_mnha_setup.py
Priority: CLI args  >  .env file  >  built-in defaults
"""

import argparse
import getpass
import pathlib
import paramiko
import sys
import time
import textwrap

# ─── BUILT-IN DEFAULTS ────────────────────────────────────────────────────────

DEFAULTS = {
    # Management IPs (SSH targets)
    "n1_host":       "10.0.0.1",
    "n2_host":       "10.0.0.2",
    # WAN
    "n1_wan_iface":  "ge-0/0/1",
    "n2_wan_iface":  "ge-0/0/1",
    "n1_wan_ip":     "10.20.20.1/24",
    "n2_wan_ip":     "10.20.20.2/24",
    "wan_gateway":   "10.20.20.254",
    # SSH
    "username":      "admin",
    "port":          22,
}

# ─── .ENV LOADER (same logic as srx_mnha_setup.py) ───────────────────────────


def load_env_file(path: str) -> dict:
    """Parse a .env file — strips inline comments, ignores blank/comment lines."""
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
        value = value.split("#")[0].strip().strip('"').strip("'")
        env[key.strip().lower()] = value
    return env


def merge_config(env: dict) -> dict:
    """Merge .env values on top of built-in DEFAULTS."""
    merged = dict(DEFAULTS)
    for key in (
        "n1_host", "n2_host",
        "n1_wan_iface", "n2_wan_iface",
        "n1_wan_ip", "n2_wan_ip",
        "wan_gateway",
        "n1_user", "n2_user",
    ):
        if key in env:
            merged[key] = env[key]
    if "ssh_port"     in env:
        merged["port"]     = int(env["ssh_port"])
    if "ssh_password" in env:
        merged["password"] = env["ssh_password"]
    if "n1_user"      in env:
        merged["n1_user"]  = env["n1_user"]
    if "n2_user"      in env:
        merged["n2_user"]  = env["n2_user"]
    return merged


# ─── ARG PARSER ───────────────────────────────────────────────────────────────


def parse_args(cfg: dict) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Configure WAN interfaces on Juniper SRX MNHA pair",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--env-file", default="srx_mnha.env", metavar="FILE",
                   help=".env file to load settings from")

    g1 = p.add_argument_group("Node 1 (primary)")
    g1.add_argument("--n1-host",      default=cfg.get("n1_host"))
    g1.add_argument("--n1-user",      default=cfg.get("n1_user", cfg["username"]))
    g1.add_argument("--n1-wan-iface", default=cfg.get("n1_wan_iface"),  metavar="IFACE")
    g1.add_argument("--n1-wan-ip",    default=cfg.get("n1_wan_ip"),     metavar="A.B.C.D/X")

    g2 = p.add_argument_group("Node 2 (secondary)")
    g2.add_argument("--n2-host",      default=cfg.get("n2_host"))
    g2.add_argument("--n2-user",      default=cfg.get("n2_user", cfg["username"]))
    g2.add_argument("--n2-wan-iface", default=cfg.get("n2_wan_iface"),  metavar="IFACE")
    g2.add_argument("--n2-wan-ip",    default=cfg.get("n2_wan_ip"),     metavar="A.B.C.D/X")

    gs = p.add_argument_group("Shared options")
    gs.add_argument("--wan-gateway",  default=cfg.get("wan_gateway"),   metavar="IP",
                    help="Default gateway IP (upstream router) — same for both nodes")
    gs.add_argument("--port",         type=int, default=cfg["port"])
    gs.add_argument("--password",     default=cfg.get("password"),
                    help="SSH password. Omit to be prompted securely.")
    gs.add_argument("--enable-nat",   action="store_true",
                    help="Also configure source NAT (trust→untrust outbound masquerade)")
    gs.add_argument("--verify",       action="store_true",
                    help="Run 'show interfaces' and 'show route' after configuring")

    return p.parse_args()


def build_node_dicts(args: argparse.Namespace) -> tuple[dict, dict]:
    password = args.password
    if not password:
        password = getpass.getpass("SSH password (used for both nodes): ")

    node1 = {
        "host":          args.n1_host,
        "username":      args.n1_user,
        "password":      password,
        "port":          args.port,
        "wan_interface": args.n1_wan_iface,
        "wan_ip":        args.n1_wan_ip,
        "wan_gateway":   args.wan_gateway,
        "wan_desc":      "WAN-NODE1",
        "enable_nat":    args.enable_nat,
    }
    node2 = {
        "host":          args.n2_host,
        "username":      args.n2_user,
        "password":      password,
        "port":          args.port,
        "wan_interface": args.n2_wan_iface,
        "wan_ip":        args.n2_wan_ip,
        "wan_gateway":   args.wan_gateway,
        "wan_desc":      "WAN-NODE2",
        "enable_nat":    args.enable_nat,
    }
    return node1, node2


# ─── SSH HELPERS ──────────────────────────────────────────────────────────────


def ssh_connect(device: dict) -> paramiko.SSHClient:
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
    return output


# ─── COMMAND BUILDER ─────────────────────────────────────────────────────────


def build_wan_commands(node: dict) -> list[str]:
    iface   = node["wan_interface"]
    wan_ip  = node["wan_ip"]
    gateway = node["wan_gateway"]
    cmds = [
        # ── WAN physical interface ────────────────────────────────────────────
        f"set interfaces {iface} description \"{node['wan_desc']}\"",
        f"set interfaces {iface} unit 0 family inet address {wan_ip}",

        # ── Security zone — untrust ───────────────────────────────────────────
        f"set security zones security-zone untrust interfaces {iface}.0",
        # Allow ping from WAN (for ISP reachability checks) — remove if not needed
        f"set security zones security-zone untrust interfaces {iface}.0 "
        f"host-inbound-traffic system-services ping",

        # ── Default static route → WAN gateway ───────────────────────────────
        f"set routing-options static route 0.0.0.0/0 next-hop {gateway}",
    ]

    # ── Optional source NAT (trust → untrust masquerade) ─────────────────────
    if node.get("enable_nat"):
        cmds += [
            "set security nat source rule-set TRUST-TO-WAN from zone trust",
            "set security nat source rule-set TRUST-TO-WAN to zone untrust",
            "set security nat source rule-set TRUST-TO-WAN rule NAT-RULE "
            "match source-address 0.0.0.0/0",
            "set security nat source rule-set TRUST-TO-WAN rule NAT-RULE "
            "then source-nat interface",
        ]

    return cmds


# ─── CONFIG RUNNER ────────────────────────────────────────────────────────────


def run_wan_config(device: dict, config_commands: list[str]) -> str:
    checkpoint_file = "/var/tmp/pre_wan_checkpoint.conf"
    full_output = ""

    client = ssh_connect(device)
    channel = client.invoke_shell(term="vt100", width=512, height=64)

    # ── 1. Drain banner, enter Junos CLI ──────────────────────────────────────
    print("    [1] Waiting for initial prompt ...")
    wait_for_prompt(channel, expected=">", timeout=10)
    channel.send("cli\n")
    out = wait_for_prompt(channel, expected=">", timeout=10)
    full_output += out
    print("        Junos CLI prompt confirmed ✓")

    # ── 2. Enter config mode — verify [edit] ──────────────────────────────────
    print("    [2] Entering configuration mode ...")
    channel.send("configure\n")
    out = wait_for_prompt(channel, expected="[edit]", timeout=10)
    full_output += out
    if "[edit]" not in out:
        channel.close(); client.close()
        raise RuntimeError(f"Failed to enter config mode on {device['host']}")
    print("        Config mode [edit] confirmed ✓")

    # ── 3. Save checkpoint ────────────────────────────────────────────────────
    print(f"    [3] Saving checkpoint → {checkpoint_file} ...")
    channel.send(f"save {checkpoint_file}\n")
    out = wait_for_prompt(channel, expected="[edit]", timeout=10)
    full_output += out
    print(f"        Checkpoint saved ✓")
    print(f"        To restore: load override {checkpoint_file} → commit")

    # ── 4. Push set commands ──────────────────────────────────────────────────
    print(f"    [4] Pushing {len(config_commands)} WAN commands ...")
    errors_found = False
    for cmd in config_commands:
        channel.send(cmd + "\n")
        out = wait_for_prompt(channel, expected="[edit]", timeout=5)
        full_output += out
        for line in out.splitlines():
            if any(kw in line.lower() for kw in ("syntax error", "invalid", "error:")):
                print(f"        [!] {line.strip()}")
                errors_found = True

    # ── 5. Commit check then commit ───────────────────────────────────────────
    if errors_found:
        print("\n    [5] SKIPPING commit — errors in set commands.")
        channel.send("rollback 0\n")
        wait_for_prompt(channel, expected="[edit]", timeout=5)
    else:
        print("    [5] Running commit check ...")
        channel.send("commit check\n")
        out = wait_for_prompt(channel, expected="[edit]", timeout=20)
        full_output += out

        check_ok  = "configuration check succeeds" in out.lower()
        check_err = "error" in out.lower()

        if check_err or not check_ok:
            print("        [ERROR] Commit check failed — full output:")
            print("        " + "─" * 50)
            for line in out.splitlines():
                if line.strip():
                    print(f"          {line}")
            print("        " + "─" * 50)
            channel.send("rollback 0\n")
            wait_for_prompt(channel, expected="[edit]", timeout=5)
        else:
            print("        Commit check passed ✓")
            print("    [6] Committing ...")
            channel.send("commit\n")
            out = wait_for_prompt(channel, expected="commit complete", timeout=30)
            full_output += out
            if "commit complete" in out.lower():
                print("        Commit complete ✓  — WAN config is live")
                print(f"        Checkpoint: {checkpoint_file}")
            elif "error" in out.lower():
                print("        [ERROR] Commit failed — restoring checkpoint ...")
                channel.send(f"load override {checkpoint_file}\n")
                wait_for_prompt(channel, expected="[edit]", timeout=15)
                channel.send("commit\n")
                out2 = wait_for_prompt(channel, expected="commit complete", timeout=30)
                full_output += out2
                print("        Rolled back to pre-WAN checkpoint ✓" if "commit complete" in out2.lower()
                      else "        [WARNING] Rollback unclear — check device manually.")
            else:
                out2 = wait_for_prompt(channel, expected="[edit]", timeout=15)
                full_output += out2
                print("        Commit complete ✓" if "commit complete" in (out + out2).lower()
                      else "        [WARNING] Commit status unclear — run: show system commit")

    # ── 7. Exit ───────────────────────────────────────────────────────────────
    print("    [7] Exiting configuration mode ...")
    channel.send("exit\n")
    wait_for_prompt(channel, expected=">", timeout=5)
    channel.send("exit\n")
    time.sleep(1)
    channel.close()
    client.close()
    return full_output


# ─── VERIFY ───────────────────────────────────────────────────────────────────


def verify_wan(device: dict, node_label: str) -> None:
    print(f"\n  Verifying WAN on {node_label} ({device['host']}) ...")
    client = ssh_connect(device)
    for cmd in [
        f"cli -c 'show interfaces {device['wan_interface']} terse'",
        "cli -c 'show route 0.0.0.0/0'",
    ]:
        _, stdout, _ = client.exec_command(cmd, timeout=10)
        print(f"\n  --- {cmd.split('-c')[1].strip()} ---")
        print(textwrap.indent(stdout.read().decode(), "  "))
    client.close()


# ─── CONFIGURE NODE ───────────────────────────────────────────────────────────


def configure_wan_node(node: dict, node_label: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  WAN config — {node_label}  ({node['host']})")
    print(f"{'=' * 60}")

    cmds = build_wan_commands(node)

    print(f"\n  Commands to be applied on {node_label}:")
    for cmd in cmds:
        print(f"    {cmd}")

    confirm = input(f"\n  Apply to {node_label} ({node['host']})? [y/N]: ").strip().lower()
    if confirm != "y":
        print(f"  Skipped {node_label}.")
        return

    print(f"\n  Applying WAN configuration to {node_label} ...")
    try:
        output = run_wan_config(node, cmds)
        print(f"\n  --- Output from {node_label} ---")
        print(textwrap.indent(output, "  "))

        error_lines = [
            ln for ln in output.splitlines()
            if "syntax error" in ln.lower()
            or ln.strip().lower().startswith("error:")
            or "invalid input" in ln.lower()
        ]
        if error_lines:
            print(f"\n  [WARNING] Errors in {node_label} output:")
            for ln in error_lines:
                print(f"    {ln.strip()}")
        else:
            print(f"\n  [OK] WAN configured on {node_label}.")

    except Exception as exc:
        print(f"\n  [ERROR] Failed to configure {node_label}: {exc}", file=sys.stderr)
        print(f"  Skipping {node_label} — continuing to next node ...")
        # Do NOT re-raise — allow the script to continue to the next node


# ─── MAIN ─────────────────────────────────────────────────────────────────────


def main() -> None:
    # Load .env first, then parse CLI args on top
    env_file = "srx_mnha.env"
    for i, arg in enumerate(sys.argv):
        if arg == "--env-file" and i + 1 < len(sys.argv):
            env_file = sys.argv[i + 1]

    env_data = load_env_file(env_file)
    if env_data:
        print(f"  [.env] Loaded {len(env_data)} settings from '{env_file}'")
    else:
        print(f"  [.env] '{env_file}' not found — using built-in defaults")

    cfg  = merge_config(env_data)
    args = parse_args(cfg)
    NODE_1, NODE_2 = build_node_dicts(args)

    nat_note = "  (+ source NAT)" if args.enable_nat else ""

    print(f"""
╔══════════════════════════════════════════════════════════╗
║    Juniper SRX MNHA — WAN Interface Setup                ║
╚══════════════════════════════════════════════════════════╝

 Node 1  :  {NODE_1['host']}
   WAN   :  {NODE_1['wan_interface']}  →  {NODE_1['wan_ip']}{nat_note}

 Node 2  :  {NODE_2['host']}
   WAN   :  {NODE_2['wan_interface']}  →  {NODE_2['wan_ip']}{nat_note}

 Default gateway  :  {NODE_1['wan_gateway']}
""")

    configure_wan_node(NODE_1, "Node-1 (primary)")
    configure_wan_node(NODE_2, "Node-2 (secondary)")

    if args.verify or input(
        "\nRun 'show interfaces' + 'show route' on both nodes? [y/N]: "
    ).strip().lower() == "y":
        verify_wan(NODE_1, "Node-1")
        verify_wan(NODE_2, "Node-2")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
