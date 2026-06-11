# Nemo Lab File Access System
## Project Specification & Work Plan

**Project:** Open-source lab file access infrastructure for NemoCE deployments  
**Institution:** Princeton University, PRISM Facility  
**Contact:** Dan (PRISM Director), Matthew Rampant (Atlantis Labs / NemoCE vendor)  
**Role:** Software Engineering Intern — Lead Architect & Primary Engineer  
**Start Date:** May 25, 2026  
**Target Demo:** August 2026  

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Solution Overview](#2-solution-overview)
3. [System Architecture](#3-system-architecture)
4. [Network Topology](#4-network-topology)
5. [Storage Layout](#5-storage-layout)
6. [Event Specifications](#6-event-specifications)
7. [API Endpoints (Daemon)](#7-api-endpoints-daemon)
8. [Daemon Module Specifications](#8-daemon-module-specifications)
9. [Nemo CE Plugin Specification](#9-nemo-ce-plugin-specification)
10. [Samba Configuration](#10-samba-configuration)
11. [Linux System Setup](#11-linux-system-setup)
12. [Config File Format](#12-config-file-format)
13. [Systemd Unit File](#13-systemd-unit-file)
14. [Repository Structure](#14-repository-structure)
15. [Tech Stack & Dependencies](#15-tech-stack--dependencies)
16. [Work Plan](#16-work-plan)
17. [Testing Plan](#17-testing-plan)
18. [Open Questions for Dan](#18-open-questions-for-dan)
19. [Links & Resources](#19-links--resources)

---

## 1. Problem Statement

Lab instrument computers (tool PCs) at Princeton PRISM are locked down, on an isolated VLAN, and have no internet access. Researchers currently have no reliable way to access their files from these machines during a lab session. The previous approach, asking users to manually authenticate, failed because tool software vendors lock down the PC environment.

Approximately 200 researchers use the PRISM facility per year. Peer institutions running NemoCE — Cornell (~800 users), Penn, Stanford — face the same problem.

---

## 2. Solution Overview

Build two pieces of open-source software that plug into the existing NemoCE lab management system:

**1. A NemoCE Plugin** — a small Python plugin that runs inside Nemo and listens for three events: user account creation, tool login, and tool logout. When any of these fire, the plugin sends an HTTP POST to the daemon.

**2. A File Server Daemon** — a Python background service (systemd) running on the Linux file server. It receives HTTP events from the plugin and performs filesystem operations: creating user directories, managing Linux bind mounts, applying POSIX ACLs, and persisting session state to SQLite.

The mechanism: each tool PC has a permanent Samba share pointing to a session directory (`/mnt/labsessions/microscope1/`). That directory is normally empty. On tool login, the daemon creates subdirectories inside it and bind-mounts the user's private directory, their group directory, and the public directory into them. It also grants the machine account temporary POSIX ACL access to the underlying source directories. On logout, the daemon unmounts cleanly after verifying no files are open, strips the ACL entries, and marks the session closed in the SQLite state database.

`/srv/labdata/` contains only the actual data: `users/`, `groups/`, and `public/`. There are no tool-specific folders inside it. The per-machine session directories in `/mnt/labsessions/` are transient infrastructure that the daemon manages at runtime.

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    CLIENT LAYER                         │
│                                                         │
│  ┌──────────────────────┐    ┌──────────────────────┐  │
│  │  VLAN (no internet)  │    │  Princeton Net / VPN  │  │
│  │  ┌────────────────┐  │    │  ┌────────────────┐   │  │
│  │  │  Tool PC 1     │  │    │  │  User Laptop   │   │  │
│  │  │  machine creds │  │    │  │  net ID creds  │   │  │
│  │  └────────────────┘  │    │  └────────────────┘   │  │
│  │  ┌────────────────┐  │    └──────────────────────┘  │
│  │  │  Tool PC 2     │  │                               │
│  │  │  machine creds │  │                               │
│  │  └────────────────┘  │                               │
│  └──────────────────────┘                               │
└─────────────────────────────────────────────────────────┘
                    ↓ SMB/VLAN          ↓ SMB/VPN
┌─────────────────────────────────────────────────────────┐
│                   SERVICE LAYER                         │
│                                                         │
│  ┌─────────────────┐   HTTP   ┌──────────────────────┐  │
│  │   Nemo CE       │ ───────► │  File Server Daemon  │  │
│  │   (our plugin   │          │  Python · systemd    │  │
│  │    lives here)  │          │  - API server        │  │
│  │                 │          │  - UserProvisioner   │  │
│  │  user_created   │          │  - SessionManager    │  │
│  │  tool_login     │          │  - MountManager      │  │
│  │  tool_logout    │          │  - IdleMonitor       │  │
│  └─────────────────┘          │  - SambaController   │  │
│                               │  - StateDB (SQLite)  │  │
│                               │  - AuditLogger       │  │
│                               └──────────┬───────────┘  │
│                                          │ bind mount   │
│                                          │ setfacl      │
│                               ┌──────────▼───────────┐  │
│                               │  Linux File Server   │  │
│                               │  Samba + smbd        │  │
│                               │  machine accounts    │  │
│                               │  Linux groups        │  │
│                               │  disk quotas + ACLs  │  │
│                               └──────────┬───────────┘  │
└──────────────────────────────────────────┼──────────────┘
                                           │ filesystem
┌──────────────────────────────────────────▼──────────────┐
│                   STORAGE LAYER                         │
│                                                         │
│  /srv/labdata/           (permanent data storage)       │
│  ├── users/              private, quota-enforced        │
│  ├── groups/             per-PI lab, SGID               │
│  └── public/             read-only for users            │
│                                                         │
│  /mnt/labsessions/       (transient session mounts)     │
│  ├── microscope1/        bind mounts appear here        │
│  └── microscope2/        empty between sessions         │
└─────────────────────────────────────────────────────────┘
```

---

## 4. Network Topology

### VLAN (air-gapped)
- Tool PCs live here
- No internet access whatsoever
- Can only reach the file server via SMB on the local network
- Tool PCs authenticate to Samba using static machine credentials (e.g., `microscope1_machine`)
- These credentials are set once and never change

### Princeton Internal Network / VPN
- User laptops connect from here
- Must be on Princeton campus network or connected via Princeton VPN
- Users authenticate to Samba using their Princeton net ID
- This is a permanent, always-available share

### File Server
- Must be reachable from both the VLAN (for tool PCs) and Princeton internal network (for laptops)
- **Open question:** is the file server dual-homed (two network interfaces) or does Princeton IT route VPN traffic to the VLAN? Confirm with Dan and Princeton IT.

### Nemo CE Server
- **Open question:** does Nemo run on the VLAN or on the Princeton internal network? This determines where the daemon's HTTP endpoint must be reachable from. Confirm with Dan.

---

## 5. Storage Layout

### Permanent Data Storage: /srv/labdata/

This directory contains only actual user data. There are no tool-specific or machine-specific folders here.

```
/srv/labdata/
├── users/
│   ├── harry/              chmod 700, chown harry:harry
│   ├── tom/                chmod 700, chown tom:tom
│   └── sarah/              chmod 700, chown sarah:sarah
│
├── groups/
│   ├── woo_lab/            chmod 2770, chown root:woo_lab (SGID bit set)
│   └── chen_lab/           chmod 2770, chown root:chen_lab
│
└── public/
    ├── protocols/          chmod 755 (read-only for users, staff-writable)
    └── resources/          chmod 755
```

### Transient Session Mounts: /mnt/labsessions/

This directory holds per-machine session directories. Each machine directory exists permanently but is empty between sessions. Bind mounts appear and disappear here at runtime.

```
/mnt/labsessions/
├── microscope1/            Samba share [microscope1] points here
│   ├── my_files/           (empty directory, bind mount target)
│   ├── lab_shared/         (empty directory, bind mount target)
│   └── public/             (empty directory, bind mount target)
└── microscope2/            Samba share [microscope2] points here
    ├── my_files/
    ├── lab_shared/
    └── public/
```

### What the Session Looks Like After tool_login

When user `harry` (member of `woo_lab`) logs into `microscope1`, the daemon runs three bind mounts and two POSIX ACL grants:

**Bind mounts (kernel-level directory projections):**
```bash
mount --bind /srv/labdata/users/harry/    /mnt/labsessions/microscope1/my_files/
mount --bind /srv/labdata/groups/woo_lab/ /mnt/labsessions/microscope1/lab_shared/
mount --bind /srv/labdata/public/         /mnt/labsessions/microscope1/public/
```

**POSIX ACL grants (temporary VFS permissions):**
```bash
setfacl -m u:microscope1_machine:rwx /srv/labdata/users/harry/
setfacl -m u:microscope1_machine:r-x /srv/labdata/groups/woo_lab/
setfacl -m u:microscope1_machine:r-x /srv/labdata/public/
```

The tool PC's SMB connection to `\\fileserver\microscope1` now shows three folders: `my_files`, `lab_shared`, and `public`. Samba sees them as native directories — no `wide links`, no reload required.

On logout, the daemon runs the reverse:
```bash
umount /mnt/labsessions/microscope1/my_files
umount /mnt/labsessions/microscope1/lab_shared
umount /mnt/labsessions/microscope1/public

setfacl -x u:microscope1_machine /srv/labdata/users/harry/
setfacl -x u:microscope1_machine /srv/labdata/groups/woo_lab/
setfacl -x u:microscope1_machine /srv/labdata/public/
```

### Why POSIX ACLs Are Required

Harry's directory is `chmod 700, chown harry:harry`. Without an ACL, the Linux kernel will deny `microscope1_machine` at the VFS layer before Samba can even respond, regardless of Samba configuration. POSIX ACLs are a kernel-level extension to standard Unix permissions that grant named users temporary, session-scoped access without changing the base ownership or mode.

### Default ACLs and File Ownership

There is a secondary ownership problem that the session ACLs alone do not solve. When `microscope1_machine` writes a file inside harry's directory, the Linux kernel stamps that file as owned by `microscope1_machine`, not by `harry`. Later, when harry connects from his laptop over VPN and authenticates as himself, he cannot read that file because he does not own it and there is no ACL granting him access to it.

The fix is a **default ACL** set once at provisioning time:

```bash
setfacl -d -m u:harry:rwx /srv/labdata/users/harry/
```

The `-d` flag makes this a directory default. Any file or subdirectory created inside `users/harry/` — by any process, any user, any machine account — will automatically inherit an ACL entry granting `harry` full access. This runs once in `UserProvisioner.provision()` and never needs to be touched again.

### Concurrent Session Handling

If harry logs into both microscope1 and microscope2 simultaneously, both machine accounts receive ACL entries:

```bash
# After logging into microscope1:
setfacl -m u:microscope1_machine:rwx /srv/labdata/users/harry/

# After logging into microscope2:
setfacl -m u:microscope2_machine:rwx /srv/labdata/users/harry/

# Logout from microscope1 — only removes microscope1's ACL:
setfacl -x u:microscope1_machine /srv/labdata/users/harry/
# microscope2 session continues unaffected
```

The `SessionManager` in the daemon tracks which machines a user is logged into, ensuring ACLs are only stripped when the user's last active session on a given machine ends.

### Linux Group Permission Model

```
/srv/labdata/groups/woo_lab/   chmod 2770   (drwxrws---)
                                             ^ SGID bit: new files inherit woo_lab group
```

Any user added to the `woo_lab` Linux group can read and write here. New files automatically inherit the group. This is standard Linux SGID behavior and requires no custom code.

### SQLite Session State Database

The daemon persists all active session state to a SQLite database before returning HTTP 200 to Nemo. If the daemon crashes or the server reboots, the startup routine reads this database, identifies orphaned sessions (bind mounts still attached, ACLs still in place), and cleans them up before accepting new requests.

```
/var/lib/labfiles/sessions.db

Table: sessions
─────────────────────────────────────────────────────
user_id     TEXT
machine_id  TEXT
group_id    TEXT
status      TEXT   ('active' | 'unmounting' | 'closed')
created_at  TEXT   (ISO timestamp)
updated_at  TEXT   (ISO timestamp)
─────────────────────────────────────────────────────
```

### Quota

Each user directory in `users/` gets an enforced disk quota set at provisioning time. Configurable via `config.yaml`. Recommended starting point: 10 GB soft limit, 12 GB hard limit.

---

## 6. Event Specifications

### user_created

**When it fires:** A lab manager adds a new user account to Nemo for the first time.

**What the plugin sends to the daemon:**

```json
POST /provision
{
  "event": "user_created",
  "user_id": "harry",
  "full_name": "Harry Smith",
  "email": "harry@princeton.edu",
  "group_id": "woo_lab"
}
```

> **Note:** Whether `group_id` is included in Nemo's `user_created` event payload is an **open question**. If it is not, the daemon will need to either query Nemo's PostgreSQL database directly or Dan will maintain a separate group mapping config. Confirm with Dan before implementing.

**What the daemon does:**
1. Create Linux system user: `useradd -r -s /usr/sbin/nologin harry`
2. Create directory: `mkdir /srv/labdata/users/harry`
3. Set ownership: `chown harry:harry /srv/labdata/users/harry`
4. Set permissions: `chmod 700 /srv/labdata/users/harry`
5. Set default ACL so harry owns all files written into his directory by machine accounts: `setfacl -d -m u:harry:rwx /srv/labdata/users/harry/`
6. Add to group: `usermod -aG woo_lab harry`
7. Set disk quota: `setquota -u harry 10240 12288 0 0 /srv/labdata`
8. Log the event

---

### tool_login

**When it fires:** A user signs into Nemo and selects one or more tools. If they select two tools, this event fires **twice** — once per tool.

**What the plugin sends:**

```json
POST /mount
{
  "event": "tool_login",
  "user_id": "harry",
  "machine_id": "microscope1",
  "session_id": "session_abc123"
}
```

**What the daemon does:**
1. Write session to SQLite with status `active` BEFORE any filesystem operations
2. Add to SessionManager: `sessions["harry"].append("microscope1")`
3. Look up harry's group from config or user record
4. Run bind mounts into `/mnt/labsessions/microscope1/`:
   - `mount --bind /srv/labdata/users/harry/ /mnt/labsessions/microscope1/my_files/`
   - `mount --bind /srv/labdata/groups/woo_lab/ /mnt/labsessions/microscope1/lab_shared/`
   - `mount --bind /srv/labdata/public/ /mnt/labsessions/microscope1/public/`
5. Grant POSIX ACLs to the machine account on the source directories:
   - `setfacl -m u:microscope1_machine:rwx /srv/labdata/users/harry/`
   - `setfacl -m u:microscope1_machine:r-x /srv/labdata/groups/woo_lab/`
   - `setfacl -m u:microscope1_machine:r-x /srv/labdata/public/`
6. No `smbcontrol reload-config` needed — Samba sees bind-mounted directories natively
7. Log the event

---

### tool_logout

**When it fires:** User releases the tool in Nemo (session ends).

**What the plugin sends:**

```json
POST /unmount
{
  "event": "tool_logout",
  "user_id": "harry",
  "machine_id": "microscope1",
  "session_id": "session_abc123"
}
```

**What the daemon does (graceful disconnect sequence):**
1. Update SQLite session status to `unmounting`
2. Check for open file handles: `smbstatus` + `lsof /mnt/labsessions/microscope1/`
3. If open handles exist: wait `IDLE_TIMEOUT` seconds, re-check
4. Unmount the three bind mounts:
   - `umount /mnt/labsessions/microscope1/my_files`
   - `umount /mnt/labsessions/microscope1/lab_shared`
   - `umount /mnt/labsessions/microscope1/public`
5. Strip POSIX ACLs from source directories:
   - `setfacl -x u:microscope1_machine /srv/labdata/users/harry/`
   - `setfacl -x u:microscope1_machine /srv/labdata/groups/woo_lab/`
   - `setfacl -x u:microscope1_machine /srv/labdata/public/`
6. Remove from SessionManager: `sessions["harry"].remove("microscope1")`
7. Mark session `closed` in SQLite
8. Log the event

> **Note on concurrent sessions:** If harry is still logged into microscope2, step 5 must NOT strip the group/public ACLs since microscope2_machine still needs them. Only strip an ACL entry for a given machine account when that machine has no remaining active sessions for this user.

---

## 7. API Endpoints (Daemon)

All endpoints require header: `X-API-Key: {configured_secret}`

| Method | Endpoint | Handler | Description |
|--------|----------|---------|-------------|
| POST | `/provision` | UserProvisioner | Handle user_created |
| POST | `/mount` | SessionManager + MountManager | Handle tool_login |
| POST | `/unmount` | IdleMonitor + MountManager | Handle tool_logout |
| GET | `/health` | — | Health check, returns 200 OK |
| GET | `/sessions` | SessionManager | Debug: list active sessions |

---

## 8. Daemon Module Specifications

### api/routes.py (API Server)
- Flask or FastAPI application
- Listens on `0.0.0.0:{config.port}` (default 5000)
- Validates API key on every request via middleware
- Returns 401 on missing/wrong key
- Returns 400 on malformed payload or sanitization failure
- Returns 200 on success with JSON confirmation
- Runs in its own thread; all module calls are synchronous within request handling

**Input sanitization (security-critical):** Every `user_id`, `machine_id`, and `group_id` from the JSON payload must be validated against a strict allowlist regex BEFORE being passed to any module. This prevents path traversal attacks where a crafted payload like `"user_id": "../../../etc"` would cause the daemon to run `mount --bind /srv/labdata/users/../../../etc/` — exposing the server root filesystem to the VLAN.

```python
import re
SAFE_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_]+$')

def sanitize_id(value: str, field_name: str) -> str:
    """Raises ValueError if value is empty or contains anything outside [a-zA-Z0-9_]."""
    if not value or not SAFE_ID_PATTERN.match(value):
        raise ValueError(f"Invalid characters in {field_name}: '{value}'")
    return value
```

Call `sanitize_id()` on all three fields at the top of every route handler. If any field fails, return HTTP 400 immediately. Never pass raw payload values to `MountManager`, `UserProvisioner`, or any module that constructs filesystem paths or shell commands.

### modules/user_provisioner.py
```python
class UserProvisioner:
    def provision(self, user_id: str, group_id: str, full_name: str) -> bool:
        # 1. useradd -r -s /usr/sbin/nologin {user_id}
        # 2. mkdir /srv/labdata/users/{user_id}
        # 3. chown {user_id}:{user_id} ...
        # 4. chmod 700 ...
        # 5. Set default ACL — any file written into this directory by any
        #    machine account will automatically inherit an ACL granting the
        #    user full access. Fixes the file ownership mismatch bug where
        #    microscope1_machine-owned files would be unreadable by harry over VPN.
        #    setfacl -d -m u:{user_id}:rwx /srv/labdata/users/{user_id}/
        # 6. usermod -aG {group_id} {user_id}
        # 7. setquota -u {user_id} {soft} {hard} 0 0 /srv/labdata
        # 8. return True on success
```

**Edge cases to handle:**
- User already exists in Linux (idempotent — do not fail)
- Directory already exists (idempotent — do not recreate, but re-apply default ACL)
- Group does not exist yet on the file server (create it)

### modules/session_manager.py
```python
class SessionManager:
    def __init__(self):
        self._sessions: dict[str, list[str]] = {}  # {user_id: [machine_id, ...]}
        self._lock = threading.Lock()

    def add(self, user_id: str, machine_id: str) -> None
    def remove(self, user_id: str, machine_id: str) -> None
    def get_machines(self, user_id: str) -> list[str]
    def get_user(self, machine_id: str) -> str | None
    def all_sessions(self) -> dict
```

**Critical:** The session dict is accessed from multiple threads (concurrent HTTP requests). All reads and writes must acquire `self._lock`.

### modules/mount_manager.py
```python
class MountManager:
    def mount(self, user_id: str, machine_id: str, group_id: str) -> bool:
        # 1. Run three bind mounts into /mnt/labsessions/{machine_id}/
        #    mount --bind /srv/labdata/users/{user_id}/    .../my_files/
        #    mount --bind /srv/labdata/groups/{group_id}/  .../lab_shared/
        #    mount --bind /srv/labdata/public/             .../public/
        # 2. Grant POSIX ACLs on source directories to machine account:
        #    setfacl -m u:{machine_account}:rwx /srv/labdata/users/{user_id}/
        #    setfacl -m u:{machine_account}:r-x /srv/labdata/groups/{group_id}/
        #    setfacl -m u:{machine_account}:r-x /srv/labdata/public/
        # 3. Return True on success
        
    def unmount(self, user_id: str, machine_id: str, group_id: str,
                remaining_sessions: list[str]) -> bool:
        # 1. Unmount all three bind mounts from /mnt/labsessions/{machine_id}/
        #    umount .../my_files
        #    umount .../lab_shared
        #    umount .../public
        # 2. Strip POSIX ACLs for this machine account:
        #    setfacl -x u:{machine_account} /srv/labdata/users/{user_id}/
        # 3. Only strip group/public ACLs if no other machines are still in session
        #    for this user (check remaining_sessions via SessionManager)
        # 4. Return True on success
```

**Edge cases:**
- Bind mount target directory does not exist (create it before mounting)
- Source directory does not exist — user was never provisioned (return error, log warning)
- Mount point already has an active bind mount — previous session was not cleaned up (force unmount first, log warning, then proceed)
- `umount` returns `target is busy` — open file handles still present (IdleMonitor handles this upstream before calling unmount)

### modules/samba_controller.py
```python
class SambaController:
    def get_open_handles(self, machine_id: str) -> list[str]:
        # Parses smbstatus output to find open files on this share
        # Returns list of open file paths, empty list if none
        # Used by IdleMonitor before unmounting
        
    def get_connected_clients(self, machine_id: str) -> list[str]:
        # Returns list of currently connected client IPs on this share
        # Used for audit logging and debug endpoint
```

> **Note:** `smbcontrol smbd reload-config` is no longer called during mount or unmount operations. Bind mounts are native kernel-level directory projections; Samba sees them as regular directories without needing to be notified. The `SambaController` is now used only for inspecting Samba state, not for driving it.

### modules/idle_monitor.py
```python
class IdleMonitor:
    def wait_and_unmount(self, user_id: str, machine_id: str,
                         group_id: str) -> bool:
        # Runs in a background thread — does not block the HTTP response
        # 1. Poll samba_controller.get_open_handles() every CHECK_INTERVAL seconds
        # 2. After IDLE_TIMEOUT seconds with open handles, force unmount regardless
        #    and log a warning
        # 3. GHOST SESSION CHECK — re-query StateDB immediately before unmounting.
        #    If session status has changed from 'unmounting' back to 'active',
        #    the user re-logged into the same tool during the wait window.
        #    Silently abort: do NOT unmount, do NOT strip ACLs, log the abort and return.
        # 4. Call mount_manager.unmount(user_id, machine_id, group_id,
        #       session_manager.get_machines(user_id))
        # 5. Call session_manager.remove(user_id, machine_id)
        # 6. Mark session 'closed' in SQLite state DB
        # 7. Log the completed unmount
        # NOTE: No smbcontrol reload needed — umount is native and immediate
```

**Critical:** The daemon must write session state to SQLite with status `unmounting` BEFORE launching this background thread. If the daemon crashes during the wait window, the startup routine will find the `unmounting` session in SQLite and complete the unmount on restart. If the user re-logs in during the wait window, `StateDB.open_session()` will update the status back to `active`, and the ghost session check in step 3 will catch this and abort cleanly.

### modules/state_db.py
```python
class StateDB:
    """
    SQLite-backed session persistence.
    Ensures sessions survive daemon crashes and server reboots.
    Database path: /var/lib/labfiles/sessions.db
    """
    
    def __init__(self, db_path: str):
        # THREADING: check_same_thread=False is required because Flask handles
        # each HTTP request in a separate thread, and IdleMonitor runs its own
        # background threads. Without this flag, sqlite3 raises ProgrammingError
        # on any cross-thread access.
        # WAL MODE: 'PRAGMA journal_mode=WAL' (Write-Ahead Logging) allows
        # multiple concurrent readers alongside a single writer without locking
        # the entire database. Without WAL, concurrent writes raise 'database is
        # locked' errors under load.
        # WRITE LOCK: even with WAL, all writes must be serialized through a
        # threading.Lock to prevent lost-update races at the Python layer.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._lock = threading.Lock()
        self._create_table()
    
    def open_session(self, user_id: str, machine_id: str,
                     group_id: str) -> None:
        # INSERT or REPLACE with status='active', timestamps
        # Acquire self._lock before writing
        
    def begin_unmount(self, user_id: str, machine_id: str) -> None:
        # UPDATE status='unmounting'
        # Acquire self._lock before writing
        
    def close_session(self, user_id: str, machine_id: str) -> None:
        # UPDATE status='closed'
        # Acquire self._lock before writing
        
    def get_active_sessions(self) -> list[dict]:
        # SELECT * WHERE status IN ('active', 'unmounting')
        # Read-only — no lock needed under WAL mode
        # Called at daemon startup to recover orphaned sessions
        
    def get_session(self, user_id: str,
                    machine_id: str) -> dict | None:
        # SELECT single session record
        # Read-only — no lock needed under WAL mode
        # Called by IdleMonitor ghost session check before unmounting
```

**Daemon startup routine (in main.py):**
```python
def recover_orphaned_sessions(state_db, mount_manager, session_manager):
    """Run at startup before accepting requests."""
    orphans = state_db.get_active_sessions()
    for session in orphans:
        logger.warning(f"Recovering orphaned session: {session}")
        mount_manager.unmount(
            session['user_id'], session['machine_id'],
            session['group_id'], remaining_sessions=[]
        )
        session_manager.remove(session['user_id'], session['machine_id'])
        state_db.close_session(session['user_id'], session['machine_id'])
```

### modules/audit_logger.py
```python
class AuditLogger:
    # Structured JSON logging
    # Every event logged with: timestamp, event_type, user_id, machine_id, action, result
    # Output: /var/log/labfiles/daemon.log
    # Rotation: daily, keep 30 days
```

---

## 9. Nemo CE Plugin Specification

### What a NemoCE Plugin Is
NemoCE (by Atlantis Labs) has a Python plugin system. Plugins are Python classes that hook into Nemo's internal event system. When Nemo fires an event, it calls the corresponding method on any registered plugin.

Plugins live in Nemo's plugin directory and are loaded at startup.

### Plugin Structure
```python
# plugin/labfiles_plugin.py

class LabFilesPlugin:
    """
    NemoCE plugin for lab file access system.
    Sends HTTP events to the file server daemon on user lifecycle events.
    """
    
    def on_user_created(self, user_id: str, user_data: dict) -> None:
        # Fires when new user added to Nemo
        # Extract group_id from user_data (confirm field name with Dan)
        # POST to /provision
        
    def on_tool_login(self, user_id: str, tool_id: str, session_id: str) -> None:
        # Fires when user selects and logs into a tool
        # Map tool_id to machine_id via config
        # POST to /mount
        
    def on_tool_logout(self, user_id: str, tool_id: str, session_id: str) -> None:
        # Fires when user releases a tool
        # POST to /unmount
```

> **Important:** The exact method signatures depend on the NemoCE plugin API. Review the plugin examples in the Atlantis Labs GitLab repository before implementing. Ask Matthew Rampant for the correct event hook names and payload format.

### HTTP Client
```python
# plugin/http_client.py

import requests

class DaemonClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers['X-API-Key'] = api_key
        self.session.timeout = 10
    
    def provision(self, user_id, group_id, full_name) -> bool: ...
    def mount(self, user_id, machine_id, session_id) -> bool: ...
    def unmount(self, user_id, machine_id, session_id) -> bool: ...
```

### tool_id to machine_id Mapping
Nemo uses `tool_id` internally. Your plugin maps this to the Linux machine name (`microscope1`, `microscope2`) via a config entry.

---

## 10. Samba Configuration

### /etc/samba/smb.conf

```ini
[global]
   workgroup = PRINCETON
   server string = PRISM Lab File Server
   security = user
   map to guest = never
   log file = /var/log/samba/log.%m
   max log size = 50
   # wide links and follow symlinks are intentionally NOT set.
   # The bind mount approach does not require them, and enabling
   # wide links on a writable share is a security vulnerability.

[microscope1]
   path = /mnt/labsessions/microscope1
   valid users = microscope1_machine
   read only = no
   browsable = no
   create mask = 0660
   directory mask = 2770

[microscope2]
   path = /mnt/labsessions/microscope2
   valid users = microscope2_machine
   read only = no
   browsable = no
   create mask = 0660
   directory mask = 2770

[userdata]
   ; Permanent share for user laptops (via VPN)
   ; %U is replaced by the authenticated username
   path = /srv/labdata/users/%U
   valid users = %S
   read only = no
   browsable = no
   create mask = 0600
   directory mask = 0700
```

> **Key design notes:**
> - Share paths point to `/mnt/labsessions/{machine}/`, not to `/srv/labdata/`. This is the session mount directory, not the data directory.
> - `wide links` and `follow symlinks` are deliberately absent. Bind mounts are native directories to Samba; no special flags are needed, and removing them closes a known security hole.
> - The `[userdata]` share for laptops points directly to `/srv/labdata/users/%U`. Laptop users access their data permanently over VPN, independent of any session lifecycle.

---

## 11. Linux System Setup

Run once on the file server to initialize the environment.

### Install Required Packages
```bash
sudo apt update
sudo apt install samba samba-common-bin quota acl python3 python3-pip python3-venv
```

### Enable ACL Support on /srv/labdata

The filesystem hosting `/srv/labdata` must be mounted with the `acl` option for `setfacl` to work.

```bash
# Edit /etc/fstab — add 'acl' to the options for the /srv/labdata partition:
# /dev/sdX /srv/labdata ext4 defaults,usrquota,acl 0 2

# Remount to apply:
sudo mount -o remount /srv/labdata

# Verify ACL support is active:
tune2fs -l /dev/sdX | grep "Default mount options"
```

### Base Directory Structure
```bash
# Permanent data storage
sudo mkdir -p /srv/labdata/{users,groups,public}
sudo mkdir -p /srv/labdata/public/{protocols,resources}

# Public permissions
sudo chmod 755 /srv/labdata/public
sudo chmod 755 /srv/labdata/public/{protocols,resources}

# Session mount directories (one per tool PC, created once, empty between sessions)
sudo mkdir -p /mnt/labsessions/microscope1/{my_files,lab_shared,public}
sudo mkdir -p /mnt/labsessions/microscope2/{my_files,lab_shared,public}

# SQLite state database directory
sudo mkdir -p /var/lib/labfiles
```

### Linux Groups (one per PI lab)
```bash
sudo groupadd woo_lab
sudo groupadd chen_lab

sudo mkdir -p /srv/labdata/groups/woo_lab
sudo chown root:woo_lab /srv/labdata/groups/woo_lab
sudo chmod 2770 /srv/labdata/groups/woo_lab   # 2 = SGID bit

sudo mkdir -p /srv/labdata/groups/chen_lab
sudo chown root:chen_lab /srv/labdata/groups/chen_lab
sudo chmod 2770 /srv/labdata/groups/chen_lab
```

### Machine Accounts (one per tool PC)
```bash
sudo useradd -r -s /usr/sbin/nologin microscope1_machine
sudo useradd -r -s /usr/sbin/nologin microscope2_machine

# Set Samba passwords for machine accounts
sudo smbpasswd -a microscope1_machine
sudo smbpasswd -a microscope2_machine
```

### Enable Disk Quotas
```bash
# Edit /etc/fstab to add usrquota option on /srv/labdata partition
# (combine with the acl option: defaults,usrquota,acl)

# Remount and initialize
sudo mount -o remount /srv/labdata
sudo quotacheck -cum /srv/labdata
sudo quotaon /srv/labdata
```

---

## 12. Config File Format

### config/config.yaml
```yaml
server:
  host: "0.0.0.0"
  port: 5000
  api_key: "CHANGE_THIS_IN_PRODUCTION"

storage:
  base_path: "/srv/labdata"
  quota_soft_mb: 10240     # 10 GB
  quota_hard_mb: 12288     # 12 GB hard limit

sessions:
  mount_base_path: "/mnt/labsessions"   # where bind mounts are created
  db_path: "/var/lib/labfiles/sessions.db"  # SQLite state database

samba:
  status_command: "smbstatus"
  # Note: reload_command removed — bind mounts do not require smbcontrol

idle_monitor:
  timeout_seconds: 30
  check_interval_seconds: 5

logging:
  log_path: "/var/log/labfiles/daemon.log"
  level: "INFO"
  rotation_days: 30

machines:
  - id: "microscope1"
    samba_user: "microscope1_machine"
    tool_id: "tool_001"      # Nemo CE internal tool ID
  - id: "microscope2"
    samba_user: "microscope2_machine"
    tool_id: "tool_002"

groups:
  - id: "woo_lab"
    path: "/srv/labdata/groups/woo_lab"
    nemo_group_id: "grp_woo"    # Nemo CE group ID (confirm with Dan)
  - id: "chen_lab"
    path: "/srv/labdata/groups/chen_lab"
    nemo_group_id: "grp_chen"
```

---

## 13. Systemd Unit File

### systemd/labfiles-daemon.service
```ini
[Unit]
Description=Lab File Access Daemon
Documentation=https://github.com/princeton-prism/labfiles
After=network.target smbd.service
Requires=smbd.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/labfiles
ExecStart=/opt/labfiles/venv/bin/python -m daemon.main
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=labfiles-daemon

[Install]
WantedBy=multi-user.target
```

### Install the service
```bash
sudo cp systemd/labfiles-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable labfiles-daemon
sudo systemctl start labfiles-daemon
sudo systemctl status labfiles-daemon
```

---

## 14. Repository Structure

```
labfiles/
│
├── plugin/                        # NemoCE plugin (runs on Nemo server)
│   ├── __init__.py
│   ├── labfiles_plugin.py         # Main plugin class, event hooks
│   ├── event_handlers.py          # Logic per event type
│   ├── http_client.py             # HTTP POST to daemon
│   └── plugin_config.py           # Plugin-specific config loader
│
├── daemon/                        # File server daemon (runs on file server)
│   ├── __init__.py
│   ├── main.py                    # Entry point, starts Flask/FastAPI
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py              # POST /provision, /mount, /unmount
│   │   └── auth.py                # API key validation middleware
│   └── modules/
│       ├── __init__.py
│       ├── user_provisioner.py
│       ├── session_manager.py
│       ├── mount_manager.py
│       ├── samba_controller.py
│       ├── idle_monitor.py
│       ├── state_db.py            # SQLite session persistence
│       └── audit_logger.py
│
├── config/
│   ├── config.yaml                # Active config (gitignored)
│   └── config.example.yaml        # Template, checked into repo
│
├── systemd/
│   └── labfiles-daemon.service    # systemd unit file
│
├── samba/
│   └── smb.conf.template          # Samba config template with placeholders
│
├── scripts/
│   ├── setup_filesystem.sh        # Creates /srv/labdata structure
│   ├── setup_groups.sh            # Creates Linux groups
│   ├── setup_machine_accounts.sh  # Creates Samba machine accounts
│   └── setup_quotas.sh            # Enables quota on the partition
│
├── tests/
│   ├── test_user_provisioner.py
│   ├── test_mount_manager.py
│   ├── test_session_manager.py
│   ├── test_samba_controller.py
│   ├── test_idle_monitor.py
│   └── test_integration.py        # End-to-end: full login/logout cycle
│
├── docs/
│   ├── system_design.md           # System design paper (for research publication)
│   ├── deployment.md              # How to deploy at a new institution
│   └── nemo_plugin_notes.md       # Notes on NemoCE plugin API
│
├── dev/
│   ├── docker-compose.yml         # Local dev environment (Nemo CE + file server)
│   └── mock_nemo_events.py        # Script to fire fake events for testing
│
├── .env.example
├── .gitignore
├── README.md
└── requirements.txt
```

---

## 15. Tech Stack & Dependencies

### Daemon (requirements.txt)
```
flask>=3.0.0           # or fastapi>=0.110.0 + uvicorn>=0.29.0
pyyaml>=6.0
python-dotenv>=1.0
pytest>=8.0
pytest-mock>=3.12
```

### Plugin
```
requests>=2.31.0
pyyaml>=6.0
```

### System Packages (file server, apt)
```
samba
samba-common-bin    # provides smbstatus
quota
acl                 # provides setfacl, getfacl (POSIX ACL tools)
python3
python3-pip
python3-venv
```

> SQLite is part of the Python standard library (`import sqlite3`). No additional Python package required for the state database.

### Development Tools
```
Docker Desktop       # for running Nemo CE locally
Git
VS Code (recommended)
Postman or curl      # for testing daemon endpoints manually
```

---

## 16. Work Plan

**Schedule:** Fridays only, ~6–8 hours per session  
**Start:** May 29, 2026  
**Demo target:** late August 2026  
**Total sessions:** ~14 Fridays  

---

### Phase 1 — Foundation (Weeks 1–3)

**Week 1 — May 29**
- [ ] Set up Git repository with the full directory structure from Section 14
- [ ] Get Docker installed and running
- [ ] Follow Atlantis Labs tutorial to run NemoCE locally via Docker
- [ ] Read the NemoCE plugin documentation and look at existing plugin examples on GitLab
- [ ] Email Dan to confirm: (a) group_id in user_created payload, (b) Nemo server network location, (c) file server OS and whether it is provisioned

**Week 2 — June 5**
- [ ] Write `plugin/labfiles_plugin.py` skeleton with all three event hooks stubbed out
- [ ] Write `plugin/http_client.py` — the requests wrapper
- [ ] Test: trigger a fake `user_created` event in local Nemo dev instance
- [ ] Confirm the event fires and your plugin method gets called (even if it just prints to stdout)

**Week 3 — June 12**
- [ ] Write `daemon/main.py` — Flask app skeleton
- [ ] Write `daemon/api/routes.py` — POST /provision, /mount, /unmount (return 200 OK for now)
- [ ] Write `daemon/api/auth.py` — API key middleware
- [ ] Test: send a manual HTTP POST from curl to the running daemon, confirm it authenticates and responds
- [ ] Write `daemon/modules/audit_logger.py` — structured logging from day one

---

### Phase 2 — Provisioning (Weeks 4–6)

**Week 4 — June 19**
- [ ] Write `daemon/modules/user_provisioner.py`
- [ ] Implement: Linux user creation, directory creation, chmod/chown
- [ ] Test in isolation using pytest with a test user (not root — use a test directory)
- [ ] Write `tests/test_user_provisioner.py`

**Week 5 — June 26**
- [ ] Add group assignment logic to UserProvisioner
- [ ] Implement: `usermod -aG {group} {user}`
- [ ] Implement: disk quota via `setquota`
- [ ] Handle edge cases: user already exists, group does not exist
- [ ] Test full provisioning cycle end-to-end: Nemo fires event → plugin → daemon → directory created

**Week 6 — July 3**
- [ ] Confirm group_id source with Dan (in payload vs DB query)
- [ ] If DB query needed: research NemoCE PostgreSQL schema, implement read-only query
- [ ] Write `config/config.example.yaml` with full schema
- [ ] Write `scripts/setup_filesystem.sh` and test it on a local Linux VM
- [ ] Write `tests/test_integration.py` — Phase 1 integration test (provisioning only)

---

### Phase 3 — Session Management & Mounting (Weeks 7–9)

**Week 7 — July 10**
- [ ] Write `daemon/modules/session_manager.py` with thread safety
- [ ] Write `tests/test_session_manager.py` — including concurrent session test
- [ ] Write `daemon/modules/state_db.py` — SQLite session persistence
- [ ] Test: open_session, begin_unmount, close_session, get_active_sessions
- [ ] Write `daemon/modules/mount_manager.py`
- [ ] Implement: bind mounts (`mount --bind`) for tool_login

**Week 8 — July 17**
- [ ] Implement: POSIX ACL grants (`setfacl -m`) on tool_login
- [ ] Implement: bind mount removal (`umount`) and ACL stripping (`setfacl -x`) on tool_logout
- [ ] Write `daemon/modules/samba_controller.py` — `smbstatus` parsing only
- [ ] Wire together: tool_login route → StateDB.open_session → SessionManager → MountManager → ACLs
- [ ] Test: full login cycle against a local Samba instance, confirm files visible

**Week 9 — July 24**
- [ ] Test: concurrent login (same user, two tools) — verify bind mounts in both session dirs, both machine accounts have ACL entries
- [ ] Test: two different users, two different machines — verify complete isolation
- [ ] Test: logout from one tool while user still active on another — verify only that machine's mounts/ACLs removed
- [ ] Write `scripts/setup_samba.sh` and validate smb.conf template
- [ ] Write `tests/test_mount_manager.py`

---

### Phase 4 — Graceful Disconnect & Recovery (Week 10)

**Week 10 — July 31**
- [ ] Write `daemon/modules/idle_monitor.py`
- [ ] Implement: open handle polling (`smbstatus`) → `umount` → `setfacl -x` sequence
- [ ] Run IdleMonitor in a background thread — tool_logout HTTP response returns immediately
- [ ] Wire together: tool_logout route → StateDB.begin_unmount → IdleMonitor (async) → MountManager.unmount → StateDB.close_session
- [ ] Implement daemon startup orphan recovery: read active sessions from SQLite, force unmount any that are still attached
- [ ] Test: logout with open file — confirm system waits then unmounts cleanly
- [ ] Test: simulate daemon crash mid-session — restart daemon, confirm orphan recovery runs and cleans up
- [ ] Write `tests/test_idle_monitor.py`

---

### Phase 5 — Integration & Hardening (Weeks 11–12)

**Week 11 — August 7**
- [ ] Full end-to-end integration test: Nemo CE → Plugin → Daemon → Samba → file visible on tool PC
- [ ] Test: user laptop VPN access via `[userdata]` Samba share
- [ ] Write `dev/mock_nemo_events.py` — script that fires all three event types against the running daemon for demo purposes
- [ ] Add `/health` and `/sessions` endpoints to daemon

**Week 12 — August 14**
- [ ] Error handling pass: what happens if Samba is down? If the base directory is missing? If provisioning fails halfway?
- [ ] Review all subprocess calls for security (no shell=True with user input)
- [ ] Write `docs/deployment.md` — step-by-step for a new institution to deploy this
- [ ] Write `README.md`
- [ ] Code review with team

---

### Phase 6 — Demo & Documentation (Weeks 13–14)

**Week 13 — August 21**
- [ ] Deploy to Dan's dev server at Princeton (not production)
- [ ] Live test with a real Nemo CE instance and real tool PC
- [ ] Fix any issues that appear in the real environment
- [ ] Draft `docs/system_design.md` for the research publication

**Week 14 — August 28**
- [ ] Demo to Dan and team
- [ ] Record demo video for open-source repository
- [ ] Create GitHub/GitLab release with deployment scripts
- [ ] Note all remaining open questions or enhancements for post-summer contributors

---

## 17. Testing Plan

### Unit Tests (pytest)
Each module gets its own test file. Filesystem operations are tested against a temporary directory (use `tmp_path` in pytest). Subprocess calls to `mount`, `umount`, `setfacl`, `useradd`, etc. are mocked.


### Integration Tests
`test_integration.py` spins up the Flask daemon in a test process, fires HTTP POST requests simulating Nemo events, and verifies filesystem state and SQLite state at each step.

### Manual Testing Checklist
Before demo, manually verify each scenario:
- [ ] New user provisioned: directory created, correct permissions, group assigned, quota set, default ACL present (`getfacl /srv/labdata/users/harry` shows default entry)
- [ ] File written by machine account is readable by user over VPN: create a file as `microscope1_machine` inside `users/harry/`, confirm harry can read it
- [ ] User logs into one tool: three bind mounts active, POSIX ACLs granted, files visible via SMB
- [ ] User logs into two tools simultaneously: bind mounts in both session dirs, both machine accounts have ACL entries
- [ ] User logs out of one tool: that machine's mounts unmounted and ACLs stripped, other session unaffected
- [ ] User logs out with open file: system waits, then unmounts after file is closed
- [ ] Ghost session: user logs out, immediately logs back in before 30s timeout — confirm files remain accessible and no unmount occurs
- [ ] Daemon crash mid-session: restart daemon, confirm orphan recovery runs and mounts are cleaned up
- [ ] Path traversal attempt: send `user_id: ../../../etc` to `/mount`, confirm 400 returned
- [ ] Laptop user connects over VPN: direct access to their personal directory
- [ ] Provisioning called twice (idempotent): no errors, default ACL still correct

---

## 18. Open Questions for Dan

Confirm all of these before or at the kickoff meeting.

1. **Group assignment:** Does the `user_created` event payload from NemoCE include a `group_id` or equivalent PI lab identifier? If not, how should the daemon know which group to assign a new user to?

2. **Nemo server location:** Is the NemoCE server on the VLAN, or on Princeton's main internal network? This determines how the daemon's HTTP endpoint must be exposed.

3. **File server network:** Is the file server dual-homed (two network interfaces — one VLAN, one Princeton network), or does Princeton IT route traffic between the VLAN and internal network? Who manages this routing?

4. **File server OS:** What OS and version is the target file server running? (Ubuntu 22.04 LTS is recommended.)

5. **Daemon machine:** Should the Python daemon run on the same machine as Samba, or on a separate VM?

6. **User authentication for laptops:** How do user laptops authenticate to Samba? Princeton net ID via Active Directory/LDAP integration, or local Samba accounts?

7. **Quota sizes:** What disk quota should each user get by default? What about group directories?

8. **Tool-to-machine mapping:** Is there a list of all tool IDs in NemoCE and their corresponding machine hostnames? This maps `tool_id` (Nemo internal) to `machine_id` (Linux hostname) in the config.

9. **NemoCE plugin API:** Can Matthew Rampant share the plugin development guide and any existing plugin examples from the Atlantis Labs GitLab?

10. **Existing users:** Are there currently existing Nemo users who do not have file server accounts yet? If so, there will need to be a one-time backfill script to provision all of them.

---

## 19. Links & Resources

### NemoCE / Atlantis Labs
- Atlantis Labs website: https://atlantislabs.io
- NemoCE GitLab: ask Matthew Rampant for repository access
- Docker setup tutorial: available on Atlantis Labs website

### Samba
- Samba documentation home: https://www.samba.org/samba/docs/
- smb.conf man page: https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html
- smbstatus man page: https://www.samba.org/samba/docs/current/man-html/smbstatus.1.html

### Linux System Administration
- systemd service files: https://www.freedesktop.org/software/systemd/man/systemd.service.html
- Linux disk quotas: https://linux.die.net/man/8/setquota
- useradd: https://linux.die.net/man/8/useradd
- usermod: https://linux.die.net/man/8/usermod
- POSIX ACLs — setfacl: https://linux.die.net/man/1/setfacl
- POSIX ACLs — getfacl: https://linux.die.net/man/1/getfacl
- POSIX ACL guide: https://www.redhat.com/sysadmin/linux-access-control-lists
- Linux bind mounts: https://man7.org/linux/man-pages/man8/mount.8.html
- lsof: https://linux.die.net/man/8/lsof
- chmod SGID bit: https://linux.die.net/man/1/chmod

### Python
- Flask documentation: https://flask.palletsprojects.com/
- FastAPI documentation: https://fastapi.tiangolo.com/
- subprocess module: https://docs.python.org/3/library/subprocess.html
- pathlib module: https://docs.python.org/3/library/pathlib.html
- threading module: https://docs.python.org/3/library/threading.html
- sqlite3 module: https://docs.python.org/3/library/sqlite3.html
- pytest documentation: https://docs.pytest.org/

### Background Reading
- SMB/CIFS protocol overview: https://wiki.samba.org/index.php/CIFS_Concepts
- Linux file permissions deep dive: https://linuxhandbook.com/linux-file-permissions/
- systemd for Python developers: https://trstringer.com/systemd-python-service/
- Event-driven architecture basics: https://martinfowler.com/articles/201701-event-driven.html

---

*Document version 3.0 — May 2026*  
*Author: Abubaker Ahmadi, Software Engineering Intern, Princeton PRISM*  
*Status: Draft — pending answers to Section 18 open questions*  
*Changes from v1.0: Replaced symlink mechanism with Linux bind mounts. Replaced chmod 700 + wide links with POSIX ACLs for VFS permission correctness. Added SQLite session state persistence for crash recovery. Removed /srv/labdata/shares/ directory. Added /mnt/labsessions/ as transient session mount location. Removed smbcontrol reload-config from mount/unmount flow.*  
*Changes from v2.0: Added default ACL (setfacl -d) at provisioning to fix file ownership mismatch when machine accounts write files. Added ghost session race condition check in IdleMonitor — thread aborts if session status returns to active before unmount. Added SQLite threading fixes: check_same_thread=False and PRAGMA journal_mode=WAL. Added input sanitization in api/routes.py to prevent path traversal injection.*
