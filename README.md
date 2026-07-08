# Juniper SRX MNHA Automation

Python scripts to configure a **Juniper SRX Multi-Node High Availability (MNHA)** pair over SSH.

---

## Folder Structure

```
srx_mnha/
├── srx_mnha.env        ← Shared configuration file (edit this before running)
├── srx_mnha_setup.py   ← Script 1: ICL + LAN setup only
├── srx_wan_setup.py    ← Script 2: WAN setup only
├── srx_full_setup.py   ← Script 3: ICL + LAN + WAN in one SSH session (recommended)
├── requirements.txt    ← Python dependencies
└── README.md           ← This file
```

---

## Prerequisites

- Python 3.10+
- Install dependencies: `pip install -r requirements.txt`
- SSH access to both SRX management IPs
- Junos OS 21.x or later
- Platform: SRX1500, SRX4100, SRX4200, SRX4600

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/PyContriver/srx_mnha.git
cd srx_mnha

# 2. Install dependencies
pip install -r requirements.txt

# 3. Edit the env file with your values
vim srx_mnha.env

# 4. Run full setup in one shot (recommended)
python3 srx_full_setup.py

# — OR — run each stage separately:
python3 srx_mnha_setup.py   # ICL + LAN only
python3 srx_wan_setup.py    # WAN only
```

---

## Configuration — `srx_mnha.env`

All settings live in one file. Edit before running either script.

| Variable | Default | Description |
|---|---|---|
| `N1_HOST` | `10.0.0.1` | Management IP — Node 1 (SSH target) |
| `N2_HOST` | `10.0.0.2` | Management IP — Node 2 (SSH target) |
| `N1_USER` | `admin` | SSH username — Node 1 |
| `N2_USER` | `admin` | SSH username — Node 2 |
| `SSH_PORT` | `22` | SSH port (same for both) |
| `SSH_PASSWORD` | *(prompt)* | SSH password — leave commented to prompt securely |
| `N1_ICL_IFACE` | `ge-0/0/0` | ICL interface — Node 1 |
| `N2_ICL_IFACE` | `ge-0/0/0` | ICL interface — Node 2 |
| `N1_ICL_IP` | `10.255.255.1/30` | ICL IP — Node 1 |
| `N2_ICL_IP` | `10.255.255.2/30` | ICL IP — Node 2 |
| `N1_LAN_IFACE` | `ge-0/0/2` | LAN interface — Node 1 |
| `N2_LAN_IFACE` | `ge-0/0/2` | LAN interface — Node 2 |
| `N1_LAN_IP` | `10.10.10.1/24` | LAN IP — Node 1 |
| `N2_LAN_IP` | `10.10.10.2/24` | LAN IP — Node 2 |
| `N1_WAN_IFACE` | `ge-0/0/1` | WAN interface — Node 1 |
| `N2_WAN_IFACE` | `ge-0/0/1` | WAN interface — Node 2 |
| `N1_WAN_IP` | `10.20.20.1/24` | WAN IP — Node 1 |
| `N2_WAN_IP` | `10.20.20.2/24` | WAN IP — Node 2 |
| `WAN_GATEWAY` | `10.20.20.254` | Default gateway (upstream router) |

### IP Address Plan

| Purpose | Subnet | Node 1 | Node 2 |
|---|---|---|---|
| Management (SSH) | `10.0.0.0/24` | `10.0.0.1` | `10.0.0.2` |
| ICL (inter-node link) | `10.255.255.0/30` | `10.255.255.1` | `10.255.255.2` |
| LAN (downstream) | `10.10.10.0/24` | `10.10.10.1` | `10.10.10.2` |
| WAN (upstream) | `10.20.20.0/24` | `10.20.20.1` | `10.20.20.2` |

---

## Script 3 — `srx_full_setup.py` (ICL + LAN + WAN — recommended)

Applies everything in **one SSH session per node** with a single checkpoint and commit.

### Commands pushed per node

```
# ICL interface
set interfaces ge-0/0/0 description "MNHA-ICL-to-peer"
set interfaces ge-0/0/0 unit 0 family inet address 10.255.255.1/30

# MNHA chassis high-availability
set chassis high-availability local-id 1
set chassis high-availability peer-id 2 peer-ip 10.255.255.2
set chassis high-availability peer-id 2 interface ge-0/0/0
set chassis high-availability peer-id 2 liveness-detection minimum-interval 1000
set chassis high-availability peer-id 2 liveness-detection multiplier 3

# LAN interface
set interfaces ge-0/0/2 description "LAN-NODE1"
set interfaces ge-0/0/2 unit 0 family inet address 10.10.10.1/24
set security zones security-zone trust interfaces ge-0/0/2.0
set security zones security-zone trust interfaces ge-0/0/2.0 host-inbound-traffic system-services ping
set security zones security-zone trust interfaces ge-0/0/2.0 host-inbound-traffic system-services ssh

# WAN interface
set interfaces ge-0/0/1 description "WAN-NODE1"
set interfaces ge-0/0/1 unit 0 family inet address 10.20.20.1/24
set security zones security-zone untrust interfaces ge-0/0/1.0
set security zones security-zone untrust interfaces ge-0/0/1.0 host-inbound-traffic system-services ping

# Default route
set routing-options static route 0.0.0.0/0 next-hop 10.20.20.254
```

### Run

```bash
# Full setup
python3 srx_full_setup.py

# Full setup + source NAT (LAN → WAN masquerade)
python3 srx_full_setup.py --enable-nat

# With post-config verification (interfaces, route, HA state, zones)
python3 srx_full_setup.py --verify

python3 srx_full_setup.py --help
```

---

## Script 1 — `srx_mnha_setup.py` (ICL + LAN)

Configures the MNHA control plane and LAN interfaces on both nodes.

### Commands pushed per node

```
# ICL interface
set interfaces ge-0/0/0 description "MNHA-ICL-to-peer"
set interfaces ge-0/0/0 unit 0 family inet address 10.255.255.1/30

# MNHA chassis high-availability
set chassis high-availability local-id 1
set chassis high-availability peer-id 2 peer-ip 10.255.255.2
set chassis high-availability peer-id 2 interface ge-0/0/0
set chassis high-availability peer-id 2 liveness-detection minimum-interval 1000
set chassis high-availability peer-id 2 liveness-detection multiplier 3

# LAN interface
set interfaces ge-0/0/2 description "LAN-NODE1"
set interfaces ge-0/0/2 unit 0 family inet address 10.10.10.1/24
set security zones security-zone trust interfaces ge-0/0/2.0
set security zones security-zone trust interfaces ge-0/0/2.0 host-inbound-traffic system-services ping
set security zones security-zone trust interfaces ge-0/0/2.0 host-inbound-traffic system-services ssh
```

### Run

```bash
# Using env file (recommended)
python3 srx_mnha_setup.py

# Override specific values
python3 srx_mnha_setup.py --n1-host 10.0.0.10 --n2-host 10.0.0.20

# With post-config verification
python3 srx_mnha_setup.py --verify

# Help
python3 srx_mnha_setup.py --help
```

---

## Script 2 — `srx_wan_setup.py` (WAN)

Configures WAN interfaces, untrust security zone, and default route on both nodes.

### Commands pushed per node

```
# WAN interface
set interfaces ge-0/0/1 description "WAN-NODE1"
set interfaces ge-0/0/1 unit 0 family inet address 10.20.20.1/24

# Security zone — untrust
set security zones security-zone untrust interfaces ge-0/0/1.0
set security zones security-zone untrust interfaces ge-0/0/1.0 host-inbound-traffic system-services ping

# Default route
set routing-options static route 0.0.0.0/0 next-hop 10.20.20.254
```

### Run

```bash
# Basic WAN setup
python3 srx_wan_setup.py

# With source NAT (LAN → WAN masquerade for internet access)
python3 srx_wan_setup.py --enable-nat

# With verification (show interfaces + show route)
python3 srx_wan_setup.py --verify

# Help
python3 srx_wan_setup.py --help
```

---

## How Both Scripts Work

Each script follows the same safe flow:

```
SSH connect
    │
    ▼
Enter Junos CLI (cli)
    │
    ▼
Enter config mode (configure) — wait for [edit] prompt
    │
    ▼
Save checkpoint → /var/tmp/pre_*_checkpoint.conf
    │
    ▼
Push set commands (one by one, check each for errors)
    │
    ├── Errors found → rollback 0, print checkpoint restore command
    │
    └── No errors →
            commit check  (dry-run validation)
                │
                ├── FAIL → rollback 0, print full error output
                │
                └── PASS → commit → config is live ✓
                                │
                             commit error → auto-restore from checkpoint
```

### Manual Rollback

If you need to revert after a successful commit:

```
> configure
# load override /var/tmp/pre_mnha_checkpoint.conf
# commit
```

---

## Configuration Priority

Settings are resolved in this order (highest wins):

```
CLI argument  >  srx_mnha.env  >  built-in defaults
```

---

## Security Note

`SSH_PASSWORD` in `.env` is commented out by default. Leaving it commented means the script will prompt you securely at runtime (password not stored anywhere). Only uncomment it if you have a specific automation requirement, and ensure the `.env` file is in `.gitignore`.

```bash
echo "srx_mnha.env" >> .gitignore
```
