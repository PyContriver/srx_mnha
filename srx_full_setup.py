#!/usr/bin/env python3
"""
Juniper SRX MNHA — Full Setup Script (ICL + LAN + WAN)
=======================================================
Combines srx_mnha_setup.py (ICL + LAN) and srx_wan_setup.py (WAN)
into a single SSH session per node:

  Single connection per node
    ├── Checkpoint saved
    ├── ICL interface + IP
    ├── MNHA chassis high-availability (local-id, peer-id, liveness-detection)
    ├── LAN interface + IP + trust zone
    ├── WAN interface + IP + untrust zone
    ├── Default static route
    ├── Source NAT (optional, --enable-nat)
    ├── commit check
    └── commit

All settings read from srx_mnha.env  (same file as the individual scripts).
Priority: CLI args  >  srx_mnha.env  >  built-in defaults
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
    # Management
    "n1_host":       "10.0.0.1",
    "n2_host":       "10.0.0.2",
    "username":      "admin",
    "port":          22,
    # ICL
    "icl_interface": "ge-0/0/0",
    "n1_icl_ip":     "10.255.255.1/30",
    "n2_icl_ip":     "10.255.255.2/30",
    # LAN
    "lan_interface": "ge-0/0/2",
    "n1_lan_ip":     "10.10.10.1/24",
    "n2_lan_ip":     "10.10.10.2/24",
    # WAN
    "wan_interface": "ge-0/0/1",
    "n1_wan_ip":     "10.20.20.1/24",
    "n2_wan_ip":     "10.20.20.2/24",
    "wan_gateway":   "10.20.20.254",
}

# ─── .ENV LOADER ─────────────────────────────────────────────────────────────


def load_env_file(path: str) -> dict:
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
    merged = dict(DEFAULTS)
    simple_keys = [
        "n1_host", "n2_host", "wan_gateway",
        "n1_icl_ip", "n2_icl_ip",
        "n1_lan_ip", "n2_lan_ip",
        "n1_wan_ip", "n2_wan_ip",
    ]
    for k in simple_keys:
        if k in env:
            merged[k] = env[k]
    # Per-node interface overrides
    for attr, default_key in [
        ("n1_icl_iface", "icl_interface"), ("n2_icl_iface", "icl_interface"),
        ("n1_lan_iface", "lan_interface"), ("n2_lan_iface", "lan_interface"),
        ("n1_wan_iface", "wan_interface"), ("n2_wan_iface", "wan_interface"),
        ("n1_user",      "username"),      ("n2_user",      "username"),
    ]:
        if attr in env:
            merged[attr] = env[attr]
    if "ssh_port"     in env:
        merged["port"]     = int(env["ssh_port"])
    if "ssh_password" in env:
        merged["password"] = env["ssh_password"]
    return merged


# ─── ARG PARSER ──────────────────────────────────────────────────────────────


def parse_args(cfg: dict) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Full SRX MNHA setup — ICL + LAN + WAN in one SSH session",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--env-file", default="srx_mnha.env")

    g1 = p.add_argument_group("Node 1 (primary)")
    g1.add_argument("--n1-host",      default=cfg["n1_host"])
    g1.add_argument("--n1-user",      default=cfg.get("n1_user", cfg["username"]))
    g1.add_argument("--n1-icl-ip",    default=cfg["n1_icl_ip"],    metavar="A.B.C.D/30")
    g1.add_argument("--n1-icl-iface", default=cfg.get("n1_icl_iface", cfg["icl_interface"]))
    g1.add_argument("--n1-lan-ip",    default=cfg["n1_lan_ip"],    metavar="A.B.C.D/X")
    g1.add_argument("--n1-lan-iface", default=cfg.get("n1_lan_iface", cfg["lan_interface"]))
    g1.add_argument("--n1-wan-ip",    default=cfg["n1_wan_ip"],    metavar="A.B.C.D/X")
    g1.add_argument("--n1-wan-iface", default=cfg.get("n1_wan_iface", cfg["wan_interface"]))

    g2 = p.add_argument_group("Node 2 (secondary)")
    g2.add_argument("--n2-host",      default=cfg["n2_host"])
    g2.add_argument("--n2-user",      default=cfg.get("n2_user", cfg["username"]))
    g2.add_argument("--n2-icl-ip",    default=cfg["n2_icl_ip"],    metavar="A.B.C.D/30")
    g2.add_argument("--n2-icl-iface", default=cfg.get("n2_icl_iface", cfg["icl_interface"]))
    g2.add_argument("--n2-lan-ip",    default=cfg["n2_lan_ip"],    metavar="A.B.C.D/X")
    g2.add_argument("--n2-lan-iface", default=cfg.get("n2_lan_iface", cfg["lan_interface"]))
    g2.add_argument("--n2-wan-ip",    default=cfg["n2_wan_ip"],    metavar="A.B.C.D/X")
    g2.add_argument("--n2-wan-iface", default=cfg.get("n2_wan_iface", cfg["wan_interface"]))

    gs = p.add_argument_group("Shared options")
    gs.add_argument("--wan-gateway",  default=cfg["wan_gateway"])
    gs.add_argument("--port",         type=int, default=cfg["port"])
    gs.add_argument("--password",     default=cfg.get("password"),
                    help="SSH password — omit to prompt securely")
    gs.add_argument("--enable-nat",   action="store_true",
                    help="Add source NAT rule (trust → untrust masquerade)")
    gs.add_argument("--verify",       action="store_true",
                    help="Run show commands after configuring")

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
        "local_id":      1,
        "peer_id":       2,
        "icl_interface": args.n1_icl_iface,
        "icl_ip":        args.n1_icl_ip,
        "lan_interface": args.n1_lan_iface,
        "lan_ip":        args.n1_lan_ip,
        "wan_interface": args.n1_wan_iface,
        "wan_ip":        args.n1_wan_ip,
        "wan_gateway":   args.wan_gateway,
        "enable_nat":    args.enable_nat,
    }
    node2 = {
        "host":          args.n2_host,
        "username":      args.n2_user,
        "password":      password,
        "port":          args.port,
        "local_id":      2,
        "peer_id":       1,
        "icl_interface": args.n2_icl_iface,
        "icl_ip":        args.n2_icl_ip,
        "lan_interface": args.n2_lan_iface,
        "lan_ip":        args.n2_lan_ip,
        "wan_interface": args.n2_wan_iface,
        "wan_ip":        args.n2_wan_ip,
        "wan_gateway":   args.wan_gateway,
        "enable_nat":    args.enable_nat,
    }
    # peer_icl_ip: each node's peer-ip = the OTHER node's ICL host address
    node1["peer_icl_ip"] = node2["icl_ip"].split("/")[0]
    node2["peer_icl_ip"] = node1["icl_ip"].split("/")[0]
    return node1, node2


# ─── COMMAND BUILDER ─────────────────────────────────────────────────────────


def build_all_commands(node: dict) -> list[str]:
    """
    Return the complete ordered list of JunOS 'set' commands for one node:
      1. ICL interface
      2. MNHA chassis high-availability
      3. LAN interface + trust zone
      4. WAN interface + untrust zone
      5. Default static route
      6. Source NAT (optional)
    """
    icl   = node["icl_interface"]
    lan   = node["lan_interface"]
    wan   = node["wan_interface"]
    pid   = node["peer_id"]

    cmds = [
        # ── 1. ICL interface ─────────────────────────────────────────────────
        f"set interfaces {icl} description \"MNHA-ICL-to-peer\"",
        f"set interfaces {icl} unit 0 family inet address {node['icl_ip']}",

        # ── 2. MNHA chassis high-availability ────────────────────────────────
        f"set chassis high-availability local-id {node['local_id']}",
        f"set chassis high-availability peer-id {pid} peer-ip {node['peer_icl_ip']}",
        f"set chassis high-availability peer-id {pid} interface {icl}",
        f"set chassis high-availability peer-id {pid} liveness-detection minimum-interval 1000",
        f"set chassis high-availability peer-id {pid} liveness-detection multiplier 3",

        # ── 3. LAN interface + trust zone ─────────────────────────────────────
        f"set interfaces {lan} description \"LAN-NODE{node['local_id']}\"",
        f"set interfaces {lan} unit 0 family inet address {node['lan_ip']}",
        f"set security zones security-zone trust interfaces {lan}.0",
        f"set security zones security-zone trust interfaces {lan}.0 "
        f"host-inbound-traffic system-services ping",
        f"set security zones security-zone trust interfaces {lan}.0 "
        f"host-inbound-traffic system-services ssh",

        # ── 4. WAN interface + untrust zone ───────────────────────────────────
        f"set interfaces {wan} description \"WAN-NODE{node['local_id']}\"",
        f"set interfaces {wan} unit 0 family inet address {node['wan_ip']}",
        f"set security zones security-zone untrust interfaces {wan}.0",
        f"set security zones security-zone untrust interfaces {wan}.0 "
        f"host-inbound-traffic system-services ping",

        # ── 5. Default route ──────────────────────────────────────────────────
        f"set routing-options static route 0.0.0.0/0 next-hop {node['wan_gateway']}",
    ]

    # ── 6. Optional source NAT (trust → untrust masquerade) ──────────────────
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


# ─── SSH HELPERS ─────────────────────────────────────────────────────────────


def ssh_connect(device: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"  [SSH] Connecting to {device['host']}:{device['port']} "
          f"as {device['username']} ...")
    client.connect(
        hostname=device["host"], port=device["port"],
        username=device["username"], password=device["password"],
        look_for_keys=False, allow_agent=False, timeout=15,
    )
    print(f"  [SSH] Connected ✓")
    return client


def wait_for_prompt(channel: paramiko.Channel, expected: str,
                    timeout: float = 10.0, poll: float = 0.2) -> str:
    output, elapsed = "", 0.0
    while elapsed < timeout:
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            if expected in output:
                return output
        time.sleep(poll)
        elapsed += poll
    return output


# ─── FULL CONFIG RUNNER ───────────────────────────────────────────────────────


def run_full_config(device: dict, config_commands: list[str]) -> str:
    checkpoint_file = "/var/tmp/pre_full_setup_checkpoint.conf"
    full_output = ""

    client = ssh_connect(device)
    channel = client.invoke_shell(term="vt100", width=512, height=64)

    # ── 1. Enter Junos CLI ────────────────────────────────────────────────────
    print("    [1] Entering Junos CLI ...")
    wait_for_prompt(channel, expected=">", timeout=10)
    channel.send("cli\n")
    out = wait_for_prompt(channel, expected=">", timeout=10)
    full_output += out
    print("        CLI prompt (>) confirmed ✓")

    # ── 2. Enter config mode ──────────────────────────────────────────────────
    print("    [2] Entering configuration mode ...")
    channel.send("configure\n")
    out = wait_for_prompt(channel, expected="[edit]", timeout=10)
    full_output += out
    if "[edit]" not in out:
        channel.close(); client.close()
        raise RuntimeError(f"Failed to enter config mode on {device['host']}")
    print("        [edit] confirmed ✓")

    # ── 3. Save checkpoint ────────────────────────────────────────────────────
    print(f"    [3] Saving checkpoint → {checkpoint_file} ...")
    channel.send(f"save {checkpoint_file}\n")
    out = wait_for_prompt(channel, expected="[edit]", timeout=10)
    full_output += out
    print(f"        Checkpoint saved ✓")
    print(f"        Restore: load override {checkpoint_file} → commit")

    # ── 4. Push all commands ──────────────────────────────────────────────────
    print(f"    [4] Pushing {len(config_commands)} commands ...")
    errors_found = False
    for cmd in config_commands:
        channel.send(cmd + "\n")
        out = wait_for_prompt(channel, expected="[edit]", timeout=5)
        full_output += out
        for line in out.splitlines():
            if any(kw in line.lower() for kw in ("syntax error", "invalid", "error:")):
                print(f"        [!] {line.strip()}")
                errors_found = True

    # ── 5. Commit check ───────────────────────────────────────────────────────
    if errors_found:
        print("\n    [5] SKIPPING commit — errors detected in commands above.")
        channel.send("rollback 0\n")
        wait_for_prompt(channel, expected="[edit]", timeout=5)
        print(f"        Rolled back. Restore from: load override {checkpoint_file} → commit")
    else:
        print("    [5] Running commit check (dry-run) ...")
        channel.send("commit check\n")
        out = wait_for_prompt(channel, expected="[edit]", timeout=20)
        full_output += out

        check_ok  = "configuration check succeeds" in out.lower()
        check_err = "error" in out.lower()

        if check_err or not check_ok:
            print("        [ERROR] Commit check failed — output:")
            print("        " + "─" * 52)
            for line in out.splitlines():
                if line.strip():
                    print(f"          {line}")
            print("        " + "─" * 52)
            channel.send("rollback 0\n")
            wait_for_prompt(channel, expected="[edit]", timeout=5)

        else:
            print("        Commit check passed ✓")

            # ── 6. Commit ─────────────────────────────────────────────────────
            print("    [6] Committing ...")
            channel.send("commit\n")
            out = wait_for_prompt(channel, expected="commit complete", timeout=30)
            full_output += out

            if "commit complete" in out.lower():
                print("        Commit complete ✓  — full config is live")
                print(f"        Checkpoint: {checkpoint_file}")
            elif "error" in out.lower():
                print("        [ERROR] Commit failed — restoring checkpoint ...")
                channel.send(f"load override {checkpoint_file}\n")
                wait_for_prompt(channel, expected="[edit]", timeout=15)
                channel.send("commit\n")
                out2 = wait_for_prompt(channel, expected="commit complete", timeout=30)
                full_output += out2
                print("        Restored checkpoint ✓" if "commit complete" in out2.lower()
                      else "        [WARNING] Restore unclear — check: show system commit")
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


# ─── VERIFY ──────────────────────────────────────────────────────────────────


def verify_node(device: dict, node_label: str) -> None:
    print(f"\n  Verifying {node_label} ({device['host']}) ...")
    client = ssh_connect(device)
    checks = [
        "cli -c 'show interfaces terse'",
        "cli -c 'show route 0.0.0.0/0'",
        "cli -c 'show chassis high-availability'",
        "cli -c 'show security zones'",
    ]
    for cmd in checks:
        _, stdout, _ = client.exec_command(cmd, timeout=10)
        label = cmd.split("-c")[1].strip().strip("'")
        print(f"\n  --- {label} ---")
        print(textwrap.indent(stdout.read().decode(), "  "))
    client.close()


# ─── CONFIGURE NODE ──────────────────────────────────────────────────────────


def configure_node(node: dict, node_label: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  Full setup — {node_label}  ({node['host']})")
    print(f"{'=' * 60}")

    cmds = build_all_commands(node)

    print(f"\n  Commands to be applied ({len(cmds)} total):")
    sections = {
        "ICL interface":            lambda c: c.startswith("set interfaces") and "ICL" in c or node["icl_interface"] in c and "description" not in c and "address" in c,
        "MNHA chassis HA":          lambda c: "high-availability" in c,
        "LAN interface + trust":    lambda c: node["lan_interface"] in c or "trust" in c,
        "WAN interface + untrust":  lambda c: node["wan_interface"] in c or "untrust" in c,
        "Default route":            lambda c: "routing-options" in c,
        "Source NAT":               lambda c: "nat" in c,
    }
    for cmd in cmds:
        print(f"    {cmd}")

    nat_note = "  (+ source NAT)" if node.get("enable_nat") else ""
    confirm = input(
        f"\n  Apply full config to {node_label} ({node['host']})?{nat_note} [y/N]: "
    ).strip().lower()
    if confirm != "y":
        print(f"  Skipped {node_label}.")
        return

    print(f"\n  Applying configuration ...")
    try:
        output = run_full_config(node, cmds)
        print(f"\n  --- Raw SSH output ({node_label}) ---")
        print(textwrap.indent(output, "  "))

        error_lines = [
            ln for ln in output.splitlines()
            if "syntax error" in ln.lower()
            or ln.strip().lower().startswith("error:")
            or "invalid input" in ln.lower()
        ]
        if error_lines:
            print(f"\n  [WARNING] Errors detected on {node_label}:")
            for ln in error_lines:
                print(f"    {ln.strip()}")
        else:
            print(f"\n  [OK] Full configuration committed on {node_label}.")

    except Exception as exc:
        print(f"\n  [ERROR] {node_label}: {exc}", file=sys.stderr)
        raise


# ─── MAIN ────────────────────────────────────────────────────────────────────


def main() -> None:
    env_file = "srx_mnha.env"
    for i, arg in enumerate(sys.argv):
        if arg == "--env-file" and i + 1 < len(sys.argv):
            env_file = sys.argv[i + 1]

    env_data = load_env_file(env_file)
    print(f"  [.env] Loaded {len(env_data)} settings from '{env_file}'"
          if env_data else f"  [.env] '{env_file}' not found — using built-in defaults")

    cfg            = merge_config(env_data)
    args           = parse_args(cfg)
    NODE_1, NODE_2 = build_node_dicts(args)
    nat_note       = "  (+ source NAT)" if args.enable_nat else ""

    print(f"""
╔══════════════════════════════════════════════════════════╗
║    Juniper SRX MNHA — Full Setup (ICL + LAN + WAN)      ║
╚══════════════════════════════════════════════════════════╝

 Node 1  :  {NODE_1['host']}
   ICL   :  {NODE_1['icl_interface']}  →  {NODE_1['icl_ip']}
   LAN   :  {NODE_1['lan_interface']}  →  {NODE_1['lan_ip']}
   WAN   :  {NODE_1['wan_interface']}  →  {NODE_1['wan_ip']}{nat_note}

 Node 2  :  {NODE_2['host']}
   ICL   :  {NODE_2['icl_interface']}  →  {NODE_2['icl_ip']}
   LAN   :  {NODE_2['lan_interface']}  →  {NODE_2['lan_ip']}
   WAN   :  {NODE_2['wan_interface']}  →  {NODE_2['wan_ip']}{nat_note}

 WAN gateway  :  {NODE_1['wan_gateway']}
""")

    configure_node(NODE_1, "Node-1 (primary)")
    configure_node(NODE_2, "Node-2 (secondary)")

    if args.verify or input(
        "\nRun verification (show interfaces/route/ha/zones) on both nodes? [y/N]: "
    ).strip().lower() == "y":
        verify_node(NODE_1, "Node-1")
        verify_node(NODE_2, "Node-2")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
