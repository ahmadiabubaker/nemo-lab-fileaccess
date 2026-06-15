# Nemo Lab File Access System

## Project Specification & Work Plan

**Project:** Open-source lab file access infrastructure for NemoCE deployments
**Institution:** Princeton University, PRISM Facility

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
18. [Security Architecture](#18-security-architecture)
19. [Open Questions](#19-open-questions)
20. [Links & Resources](#20-links--resources)

---

## 1. Problem Statement

Lab instrument computers (tool PCs) at Princeton PRISM are locked down, on an isolated VLAN, and have no internet access. Researchers currently have no reliable way to access their files from these machines during a lab session. The previous approach, asking users to manually authenticate, failed because tool software vendors lock down the PC environment.

Approximately 200 researchers use the PRISM facility per year. Peer institutions running NemoCE — Cornell (~800 users), Penn, Stanford — face the same problem.

---

## 2. Solution Overview

Build two pieces of open-source software that plug into the existing NemoCE lab management system:

**1. A NemoCE Plugin** — a small Python plugin that runs inside Nemo and listens for three events: user account creation, tool login, and tool logout. When any of these fire, the plugin sends an HTTP POST to the daemon.

**2. A File Server Daemon** — a Python background service (systemd) running on the Linux file server. It receives HTTP events from the plugin and performs filesystem operations: creating user directories, managing Linux bind mounts, applying POSIX ACLs, and persisting session state to SQLite. It also runs a periodic `NemoSync` job that keeps group/membership information up to date by polling the NEMO API.

The mechanism: each tool PC has a permanent Samba share pointing to a session directory (`/mnt/labsessions/microscope1/`). That directory is normally empty. On tool login, the daemon creates subdirectories inside it and bind-mounts the user's private directory, one or more group/lab directories, and the public directory into them. It also grants the machine account temporary POSIX ACL access to the underlying source directories. On logout, the daemon unmounts cleanly after verifying no files are actively being written, strips the ACL entries, and marks the session closed in the SQLite state database.

`/srv/labdata/` contains only the actual data: `users/`, `groups/`, and `public/`. There are no tool-specific folders inside it. The per-machine session directories in `/mnt/labsessions/` are transient infrastructure that the daemon manages at runtime.

For external/off-site access (user laptops, home computers), this project pairs with a **Nextcloud** instance rather than direct SMB/VPN access — see [Section 4](#4-network-topology) and [Section 10](#10-samba-configuration).

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    CLIENT LAYER                         │
│                                                         │
│  ┌──────────────────────┐    ┌──────────────────────┐  │
│  │  VLAN (no internet)  │    │  Internet / Off-site  │  │
│  │  ┌────────────────┐  │    │  ┌────────────────┐   │  │
│  │  │  Tool PC 1     │  │    │  │  User Browser  │   │  │
│  │  │  machine creds │  │    │  │  / Nextcloud   │   │  │
│  │  └────────────────┘  │    │  │  client        │   │  │
│  │  ┌────────────────┐  │    │  └────────────────┘   │  │
│  │  │  Tool PC 2     │  │    └──────────────────────┘  │
│  │  │  machine creds │  │                               │
│  │  └────────────────┘  │                               │
│  └──────────────────────┘                               │
└─────────────────────────────────────────────────────────┘
                    ↓ SMB/VLAN          ↓ HTTPS (WebDAV)
┌─────────────────────────────────────────────────────────┐
│                   SERVICE LAYER                         │
│                                                         │
│  ┌─────────────────┐   HTTPS  ┌──────────────────────┐  │
│  │   Nemo CE       │ ───mTLS─►│  File Server Daemon  │  │
│  │   (our plugin   │          │  Python · systemd    │  │
│  │    lives here)  │          │  - API server (TLS)  │  │
│  │                 │          │  - UserProvisioner   │  │
│  │  user_created   │          │  - SessionManager    │  │
│  │  tool_login     │          │  - MountManager      │  │
│  │  tool_logout    │          │  - IdleMonitor       │  │
│  └─────────────────┘          │  - SambaController   │  │
│                               │  - NemoSync          │  │
│         ▲                     │  - StateDB (SQLite)  │  │
│         │ polls NEMO API      │  - AuditLogger       │  │
│         └─────────────────────┴──────────┬───────────┘  │
│                                          │ bind mount   │
│                                          │ setfacl      │
│                               ┌──────────▼───────────┐  │
│                               │  Linux File Server   │  │
│                               │  Samba + smbd        │  │
│                               │  + Nextcloud         │  │
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

### Off-Site / External Access via Nextcloud + SAML SSO
- Users who are off-campus (laptops, home computers) do **not** connect directly to Samba over VPN. The institution's IT department has confirmed that Samba/SMB cannot perform SSO authentication, and recommended a web-based interface that authenticates via SAML instead.
- A Nextcloud instance runs on (or alongside) the file server and exposes each user's `/srv/labdata/users/<user_id>/` directory and their group directories as Nextcloud storage (via Nextcloud's "External Storage" local filesystem backend, pointed at the same paths Samba serves).
- Users authenticate to Nextcloud via the **`user_saml` app**, configured against the institution's Shibboleth/SAML identity provider — the same SSO system NemoCE itself uses for login (Section 9). This gives a single login path for both:
  - **Internal NetID accounts** (also present in the campus AD), and
  - **Guest accounts** for external collaborators (present in Shibboleth but *not* in AD).

  Because both account types exist in Shibboleth, SAML-based login to Nextcloud works identically for both, without requiring either an AD domain join or a separate credential store on the file server.
- Access is over HTTPS — via the Nextcloud web UI, desktop sync client, or mobile app.
- Because Nextcloud reads/writes the same underlying directories as Samba, the same POSIX ownership and default-ACL scheme (Section 5) keeps files readable by the right user regardless of whether they were written by a tool PC's machine account or by Nextcloud.
- This removes the need for VPN access, Active Directory domain join, or a `[homes]`-style Samba share for laptops — and directly follows IT's recommendation (a web interface doing SAML auth, talking to the file server on the user's behalf) rather than attempting SSO over SMB.

### File Server
- **Confirmed dual-homed**: the file server has two network interfaces — one on the air-gapped VLAN (for tool PCs) and one on the institution's internal/campus network (for NemoCE, the daemon's HTTPS API, researcher laptops via Nextcloud, and admins).
- The daemon's HTTPS API binds **only** to the campus-network-facing interface (Section 18.1). Tool PCs on the VLAN communicate with the file server **only via SMB** on the VLAN interface and cannot reach the daemon port at all.
- `smbd` is bound only to the VLAN-facing interface (Section 10/18.6); Nextcloud and the daemon API are reachable only from the campus-network-facing interface.

### Nemo CE Server
- **Confirmed**: NemoCE runs in Docker on a separate server (managed by central IT), reachable from the campus/internal network — **not** on the VLAN. There is a DEV instance (campus-network/VPN-only) and a PROD instance (internet-reachable), both running NemoCE 7.4.17 with a PostgreSQL backend.
- The daemon's HTTPS endpoint must therefore be reachable from the campus-network-facing interface of the file server (consistent with the dual-homing above), and the mTLS/firewall rules between the NemoCE plugin and the daemon are scoped to that interface, not the VLAN.
- For development/testing, the DEV NemoCE instance can have fake tools created and enabled/disabled freely without any real-world side effects (no interlock control, no emails) — see Section 17 for how this is used in the testing plan.

---

## 5. Storage Layout

### The Data Reality (Background)

Real NEMO data from the facility shows:
- The hierarchy is strictly **Account (PI/lab) → Project (grant/effort) → User (researcher)**. A project belongs to exactly one account; an account can have many projects.
- **86% of users (629/733)** have projects spanning **more than one account**. Multi-group access is the norm, not an edge case.
- Within a single account, different users are often on **different subsets of that account's projects**. Project-level isolation is required: a user must not see a project's files just because they're on another project under the same account/PI.
- Raw NEMO names are unsafe as filesystem paths: account/project names contain `.`, `()`, `:`, `/`, non-breaking spaces, and run up to 134 characters; ~23% of usernames are full email addresses (some >32 chars, Linux's username length limit).
- NEMO's numeric `id` for accounts, projects, and users is stable; human-readable names/usernames can be renamed by NEMO at any time.

These facts drive the design below: **physical storage paths are ID-based and never change**, Linux groups and access control are **per-project**, and human-readable sanitized names are used only for the session-visible bind-mount names (and the equivalent Nextcloud display names) — which are cheap to regenerate on every `tool_login` / every `NemoSync` run.

### Permanent Data Storage: /srv/labdata/

This directory contains only actual data. There are no tool-specific or machine-specific folders here.

```
/srv/labdata/
├── users/
│   ├── 709/                chmod 700, chown 709:709 (Linux uid/gid = NEMO user id)
│   ├── 278/                chmod 700, chown 278:278
│   └── ...
│
├── groups/
│   ├── account_42/         chmod 0711, chown root:root (traversal only — see below)
│   │   ├── project_10/     chmod 2770, chown root:proj_10 (SGID bit set)
│   │   └── project_11/     chmod 2770, chown root:proj_11
│   ├── account_12/
│   │   └── project_17/     chmod 2770, chown root:proj_17
│   └── ...
│
└── public/
    ├── protocols/           chmod 755 (read-only for users, staff-writable)
    └── resources/           chmod 755
```

**Why `account_<id>/` is `chmod 0711, chown root:root`:** it has no group ownership tying it to any project. `0711` gives `root` full access and everyone else execute-only (traversal) permission — necessary so the kernel can resolve the path down to `project_<id>/`, but `ls account_42/` returns nothing useful to a non-root user, and a user with access to `project_10` cannot discover the existence of `project_11` by listing its parent. The actual access decision happens entirely at the `project_<id>/` level via its Linux group.

**Why `users/<id>/` is named by NEMO's numeric user `id`, not username:** usernames are sometimes full email addresses (up to 39 chars seen in the sample data, exceeding the 32-char Linux username limit) and can be changed by NEMO. The numeric `id` is stable and always a valid directory/username component. The corresponding Linux user account is also created with a numeric username derived this way (e.g. `u709`) — see Section 6.

### Transient Session Mounts: /mnt/labsessions/

This directory holds per-machine session directories. Each machine directory exists permanently but is empty between sessions. Bind mounts appear and disappear here at runtime.

For each `(account, project)` pair the user has access to, the daemon creates **one bind mount**, named using a **sanitized, human-readable** combination of the account and project names — e.g. `lab_shared_woo_lab_quantum_sensing`. This name is recomputed from the current NEMO names every time it's mounted, so renames in NEMO are reflected on the user's next `tool_login` without ever touching the underlying `/srv/labdata/groups/account_<id>/project_<id>/` path.

```
/mnt/labsessions/
├── microscope1/                              Samba share [microscope1] points here
│   ├── my_files/                             (empty directory, bind mount target)
│   ├── lab_shared_woo_lab_quantum_sensing/   (bind mount target — account_42/project_10)
│   ├── lab_shared_woo_lab_nv_centers/        (bind mount target — account_42/project_11,
│   │                                           only if this user is ALSO on project_11)
│   ├── lab_shared_chen_lab_2d_materials/     (bind mount target — account_12/project_17)
│   └── public/                               (empty directory, bind mount target)
└── microscope2/                              Samba share [microscope2] points here
    ├── my_files/
    ├── lab_shared_woo_lab_quantum_sensing/
    └── public/
```

### Sanitization Algorithm

Used to turn a NEMO account/project name into a safe, readable path component for bind-mount names (and, in Section 7, Nextcloud folder labels). Applied fresh every time a name is needed — never persisted as a physical path.

```python
import re

def sanitize_name(name: str, max_len: int = 40) -> str:
    """
    1. Lowercase
    2. Replace any run of characters outside [a-z0-9] with a single '_'
    3. Strip leading/trailing '_'
    4. Truncate to max_len characters
    5. If empty after the above, fall back to "unnamed"
    """
    s = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    s = s[:max_len].strip('_')
    return s or "unnamed"
```

A bind-mount name is built as `lab_shared_{sanitize_name(account.name)}_{sanitize_name(project.name)}`, truncated again to a reasonable total length (e.g. 80 chars) if needed. Because this name is **session-scoped and ephemeral** (it exists only while the bind mount is active), **collisions between two different `(account, project)` pairs producing the same sanitized name are handled by appending the project's numeric `id`** (e.g. `lab_shared_woo_lab_..._p11`) only in the rare case both names sanitize identically — this is a display-only disambiguation and never affects the underlying `/srv/labdata/groups/account_<id>/project_<id>/` path.

### What the Session Looks Like After tool_login

User `709` has access to `account_42/project_10`, `account_42/project_11`, and `account_12/project_17`. When they log into `microscope1`, the daemon runs:

**Bind mounts (kernel-level directory projections):**
```bash
mount --bind /srv/labdata/users/709/                       /mnt/labsessions/microscope1/my_files/
mount --bind /srv/labdata/groups/account_42/project_10/    /mnt/labsessions/microscope1/lab_shared_woo_lab_quantum_sensing/
mount --bind /srv/labdata/groups/account_42/project_11/    /mnt/labsessions/microscope1/lab_shared_woo_lab_nv_centers/
mount --bind /srv/labdata/groups/account_12/project_17/    /mnt/labsessions/microscope1/lab_shared_chen_lab_2d_materials/
mount --bind /srv/labdata/public/                          /mnt/labsessions/microscope1/public/
```

**POSIX ACL grants (temporary VFS permissions):**
```bash
setfacl -m u:microscope1_machine:rwx /srv/labdata/users/709/
setfacl -m u:microscope1_machine:r-x /srv/labdata/groups/account_42/project_10/
setfacl -m u:microscope1_machine:r-x /srv/labdata/groups/account_42/project_11/
setfacl -m u:microscope1_machine:r-x /srv/labdata/groups/account_12/project_17/
setfacl -m u:microscope1_machine:r-x /srv/labdata/public/
```

> Note: the daemon does **not** need to (and must not) grant `microscope1_machine` any ACL on `account_42/` or `account_12/` themselves — the bind mount targets `project_<id>/` directly, and the kernel resolves the bind-mount source path with the daemon's (root) privileges, not the querying process's. The machine account never needs traversal rights on `account_<id>/`.

The tool PC's SMB connection to `\\fileserver\microscope1` now shows one folder per `(account, project)` the user has access to, plus `my_files` and `public`. Samba sees them as native directories — no `wide links`, no reload required.

On logout, the daemon runs the reverse for every mount it created for that session:
```bash
umount /mnt/labsessions/microscope1/my_files
umount /mnt/labsessions/microscope1/lab_shared_woo_lab_quantum_sensing
umount /mnt/labsessions/microscope1/lab_shared_woo_lab_nv_centers
umount /mnt/labsessions/microscope1/lab_shared_chen_lab_2d_materials
umount /mnt/labsessions/microscope1/public

setfacl -x u:microscope1_machine /srv/labdata/users/709/
setfacl -x u:microscope1_machine /srv/labdata/groups/account_42/project_10/
setfacl -x u:microscope1_machine /srv/labdata/groups/account_42/project_11/
setfacl -x u:microscope1_machine /srv/labdata/groups/account_12/project_17/
setfacl -x u:microscope1_machine /srv/labdata/public/
```

### Group/Project Membership Source: NemoSync

Which `(account, project)` pairs a user has access to (and therefore which `lab_shared_*` mounts they get) is determined by the `NemoSync` module, which periodically polls the NEMO API and stores the result in local SQLite tables (`accounts`, `projects`, `memberships`). See [Section 8](#8-daemon-module-specifications) for details.

> **Note on `group_mapping_mode`:** earlier drafts of this README described a single `group_mapping_mode` ("account" vs. "project") that picked one level of the hierarchy as "the group." Given the real data — 86% of users span multiple accounts, and project-level isolation within an account is required — **both levels are now used together**: `account_<id>/project_<id>/` is the physical and access-control unit (Linux group per project), and the account is retained purely as a directory grouping / namespace for the bind-mount display name. `group_mapping_mode` as a single either/or config option is removed; see Section 19 for the open question this raises with MNFC (their stated preference was account-level sharing, which this design narrows to project-level — needs confirmation).

### Why POSIX ACLs Are Required

User `709`'s directory is `chmod 700, chown 709:709`. Without an ACL, the Linux kernel will deny `microscope1_machine` at the VFS layer before Samba can even respond, regardless of Samba configuration. POSIX ACLs are a kernel-level extension to standard Unix permissions that grant named users temporary, session-scoped access without changing the base ownership or mode.

### Default ACLs and File Ownership

There is a secondary ownership problem that the session ACLs alone do not solve. When `microscope1_machine` writes a file inside user `709`'s directory, the Linux kernel stamps that file as owned by `microscope1_machine`, not by `709`. Later, when the user connects via Nextcloud and authenticates as themselves, they cannot read that file because they do not own it and there is no ACL granting them access to it.

The fix is a **default ACL** set once at provisioning time:

```bash
setfacl -d -m u:709:rwx /srv/labdata/users/709/
```

The `-d` flag makes this a directory default. Any file or subdirectory created inside `users/709/` — by any process, any user, any machine account — will automatically inherit an ACL entry granting user `709` full access. This runs once in `UserProvisioner.provision()` and never needs to be touched again.

The same default-ACL pattern is applied to each `project_<id>/` directory for its Linux group, so files written by any machine account are readable by all members of that project's group via Nextcloud:

```bash
setfacl -d -m g:proj_10:rwx /srv/labdata/groups/account_42/project_10/
```

### Concurrent Session Handling

If user `709` logs into both microscope1 and microscope2 simultaneously, both machine accounts receive ACL entries:

```bash
# After logging into microscope1:
setfacl -m u:microscope1_machine:rwx /srv/labdata/users/709/

# After logging into microscope2:
setfacl -m u:microscope2_machine:rwx /srv/labdata/users/709/

# Logout from microscope1 — only removes microscope1's ACL:
setfacl -x u:microscope1_machine /srv/labdata/users/709/
# microscope2 session continues unaffected
```

The `SessionManager` in the daemon tracks which machines a user is logged into, ensuring ACLs are only stripped when the user's last active session on a given machine ends. The same logic applies per `project_<id>/` directory: an ACL for a given machine account on a project directory is only stripped once no remaining session for that user (on that machine) requires it.

### Linux Group Permission Model

```
/srv/labdata/groups/account_42/project_10/   chmod 2770   (drwxrws---)
                                              chown root:proj_10
                                              ^ SGID bit: new files inherit the proj_10 group
```

Any user added to the `proj_10` Linux group can read and write here. New files automatically inherit the group. This is standard Linux SGID behavior and requires no custom code. A user who is a member of `account_42` (i.e., on at least one of its projects) but **not** a member of `proj_11` has no Linux group granting access to `account_42/project_11/`, and the `account_42/` parent directory's `0711` permissions prevent them from even listing it.

### SQLite Session State Database

The daemon persists all active session state to a SQLite database before returning HTTP 200 to Nemo. If the daemon crashes or the server reboots, the startup routine reads this database, identifies orphaned sessions (bind mounts still attached, ACLs still in place), and cleans them up before accepting new requests.

```
/var/lib/labfiles/sessions.db

Table: sessions
─────────────────────────────────────────────────────
user_id      TEXT   (NEMO numeric user id)
machine_id   TEXT
project_ids  TEXT   (JSON list of NEMO numeric project ids — supports
                      multi-project mounts)
status       TEXT   ('active' | 'unmounting' | 'closed')
created_at   TEXT   (ISO timestamp)
updated_at   TEXT   (ISO timestamp)
─────────────────────────────────────────────────────

Table: accounts
─────────────────────────────────────────────────────
account_id   TEXT   (NEMO numeric account id — stable key)
name         TEXT   (current NEMO account name, for display/sanitization)
active       INTEGER (0/1, from NEMO)
updated_at   TEXT
─────────────────────────────────────────────────────

Table: projects
─────────────────────────────────────────────────────
project_id   TEXT   (NEMO numeric project id — stable key)
account_id   TEXT   (FK to accounts.account_id)
name         TEXT   (current NEMO project name, for display/sanitization)
path         TEXT   (/srv/labdata/groups/account_<account_id>/project_<project_id>)
linux_group  TEXT   (e.g. "proj_10")
active       INTEGER (0/1, from NEMO)
updated_at   TEXT
─────────────────────────────────────────────────────

Table: memberships
─────────────────────────────────────────────────────
user_id      TEXT   (NEMO numeric user id)
project_id   TEXT   (FK to projects.project_id)
updated_at   TEXT
─────────────────────────────────────────────────────
```

The `accounts`, `projects`, and `memberships` tables are maintained by `NemoSync` (Section 8), independent of the `sessions` table. Note that `group_id`/`group_ids` from earlier drafts are replaced by `project_ids` throughout — a "group" in the filesystem sense is now always a `project_<id>/` directory.

The database is opened with `PRAGMA journal_mode=WAL` and a non-zero `busy_timeout` so that concurrent reads from the API server, `IdleMonitor` background threads, and `NemoSync`'s periodic job do not raise `database is locked` errors.

### Quota

Each user directory in `users/` gets an enforced disk quota set at provisioning time, applied against the numeric Linux uid (e.g. `setquota -u 709 ...`). Configurable via `config.yaml`. Recommended starting point: 10 GB soft limit, 12 GB hard limit. Project directories may optionally have their own quotas (Section 19).

---

## 6. Event Specifications

### user_created

**When it fires:** A lab manager adds a new user account to Nemo for the first time.

**What the plugin sends to the daemon:**

```json
POST /provision
{
  "event": "user_created",
  "user_id": 709,
  "username": "harry",
  "full_name": "Harry Smith",
  "email": "harry@example.edu"
}
```

> **Note:** Group/project membership is **not** expected in this payload. Instead, `NemoSync` independently polls the NEMO API to determine which projects a user belongs to, and updates the `memberships` table. `UserProvisioner.provision()` does not need to know the user's projects at creation time — project directories and Linux group membership are reconciled by `NemoSync` on its next poll.
>
> **`user_id` is NEMO's numeric `id`, not the username:** NEMO's accounts, projects, and users tables are keyed by a numeric database `id`, and human-readable fields (username, name, description) can change over time — usernames can even be full email addresses, some exceeding Linux's 32-character username limit. The daemon therefore uses NEMO's numeric `id` directly as the Linux uid **and** as the directory name under `users/` (e.g. `/srv/labdata/users/709/`). The Linux system username is derived as `u<id>` (e.g. `u709`) to guarantee a valid, stable identifier regardless of what the NEMO username looks like. The human-readable `username`/`full_name` fields are stored only for logging/display and are not used in any filesystem path.

**What the daemon does:**
1. Create Linux system user: `useradd -r -u 709 -s /usr/sbin/nologin u709`
2. Create directory: `mkdir /srv/labdata/users/709`
3. Set ownership: `chown 709:709 /srv/labdata/users/709`
4. Set permissions: `chmod 700 /srv/labdata/users/709`
5. Set default ACL so the user owns all files written into their directory by machine accounts: `setfacl -d -m u:709:rwx /srv/labdata/users/709/`
6. Set disk quota: `setquota -u 709 10240 12288 0 0 /srv/labdata`
7. Log the event
8. (Project/group assignment happens later, via `NemoSync`)

---

### tool_login

**When it fires:** A user signs into Nemo and selects one or more tools. If they select two tools, this event fires **twice** — once per tool.

**What the plugin sends:**

```json
POST /mount
{
  "event": "tool_login",
  "user_id": 709,
  "machine_id": "microscope1",
  "session_id": "session_abc123"
}
```

**What the daemon does:**
1. Write session to SQLite with status `active` BEFORE any filesystem operations
2. Add to SessionManager: `sessions[709].append("microscope1")`
3. Look up user 709's current project memberships from the `memberships`/`projects`/`accounts` tables (maintained by `NemoSync`)
4. Run bind mounts into `/mnt/labsessions/microscope1/`:
   - `mount --bind /srv/labdata/users/709/ /mnt/labsessions/microscope1/my_files/`
   - For each project `p` (with account `a`) user 709 belongs to: `mount --bind /srv/labdata/groups/account_{a.id}/project_{p.id}/ /mnt/labsessions/microscope1/lab_shared_{sanitize(a.name)}_{sanitize(p.name)}/`
   - `mount --bind /srv/labdata/public/ /mnt/labsessions/microscope1/public/`
5. Grant POSIX ACLs to the machine account on the source directories:
   - `setfacl -m u:microscope1_machine:rwx /srv/labdata/users/709/`
   - For each project `p` (with account `a`): `setfacl -m u:microscope1_machine:r-x /srv/labdata/groups/account_{a.id}/project_{p.id}/`
   - `setfacl -m u:microscope1_machine:r-x /srv/labdata/public/`
6. No `smbcontrol reload-config` needed — Samba sees bind-mounted directories natively
7. Log the event

> **Naming note:** `sanitize(...)` is the algorithm in [Section 5](#5-storage-layout). Bind-mount names are recomputed from the current NEMO account/project names on every `tool_login` — if NEMO renames a project between sessions, the next login simply mounts under the new sanitized name, with no change to the underlying `account_<id>/project_<id>/` path.

---

### tool_logout

**When it fires:** User releases the tool in Nemo (session ends).

**What the plugin sends:**

```json
POST /unmount
{
  "event": "tool_logout",
  "user_id": 709,
  "machine_id": "microscope1",
  "session_id": "session_abc123"
}
```

**What the daemon does (graceful disconnect sequence):**
1. Update SQLite session status to `unmounting`
2. Hand off to `IdleMonitor`, which polls `SambaController.get_open_handles()` to distinguish files that are merely **open for reading** from files with an **active write handle** (see Section 8)
3. Once no write handles remain (or the idle timeout ceiling is reached for read-only handles), unmount all bind mounts created for this session:
   - `umount /mnt/labsessions/microscope1/my_files`
   - `umount /mnt/labsessions/microscope1/lab_shared_<sanitized_account>_<sanitized_project>` (for each project mount)
   - `umount /mnt/labsessions/microscope1/public`
4. Strip POSIX ACLs from source directories for this machine account:
   - `setfacl -x u:microscope1_machine /srv/labdata/users/709/`
   - `setfacl -x u:microscope1_machine /srv/labdata/groups/account_<a.id>/project_<p.id>/` (for each project, only if no other machine session still needs it)
   - `setfacl -x u:microscope1_machine /srv/labdata/public/`
5. Remove from SessionManager: `sessions[709].remove("microscope1")`
6. Mark session `closed` in SQLite
7. Log the event

> **Note on concurrent sessions:** If user 709 is still logged into microscope2, step 4 must NOT strip the project/public ACLs since microscope2_machine still needs them. Only strip an ACL entry for a given machine account when that machine has no remaining active sessions for this user.

> **Note on active writes:** Unlike a fixed sleep-and-retry, `IdleMonitor` never force-unmounts a directory while a file inside it has an open **write** handle — doing so could corrupt in-progress saves from instrument software. Read-only handles (e.g., a file left open in a viewer) are subject to `max_idle_timeout_seconds` and will be force-unmounted once that ceiling is reached, with a warning logged.

---

## 7. API Endpoints (Daemon)

All endpoints require:
- The connection to be HTTPS with a valid client certificate (mutual TLS — see [Section 18](#18-security-architecture))
- Header: `X-API-Key: {configured_secret}`

| Method | Endpoint | Handler | Description |
|--------|----------|---------|-------------|
| POST | `/provision` | UserProvisioner | Handle user_created |
| POST | `/mount` | SessionManager + MountManager | Handle tool_login |
| POST | `/unmount` | IdleMonitor + MountManager | Handle tool_logout |
| GET | `/health` | — | Health check, returns 200 OK |
| GET | `/sessions` | SessionManager | Debug: list active sessions |

All endpoints are also subject to rate limiting (Section 18.4) and a `machine_id`/source allowlist.

---

## 8. Daemon Module Specifications

### api/routes.py (API Server)
- Flask (or FastAPI) application
- Binds to a single configured interface/address (not `0.0.0.0`) and serves **HTTPS only**, terminating TLS with mutual client-certificate verification (see Section 18.2–18.3)
- Validates API key on every request via middleware
- Returns 401 on missing/wrong key, 403 on failed client cert verification
- Returns 400 on malformed payload or sanitization failure
- Returns 429 when rate limits are exceeded
- Returns 200 on success with JSON confirmation
- Runs in its own thread; all module calls are synchronous within request handling

**Input sanitization (security-critical):** Every `user_id`, `machine_id`, and project/account `id` from the JSON payload must be validated BEFORE being passed to any module. `user_id`, `machine_id`'s tool mapping, and project/account ids are all **numeric NEMO ids** (see Section 5/6) — the daemon never accepts a human-readable name as a path component. This prevents path traversal attacks where a crafted payload like `"user_id": "../../../etc"` would cause the daemon to run `mount --bind /srv/labdata/users/../../../etc/` — exposing the server root filesystem to the VLAN.

```python
import re
SAFE_ID_PATTERN = re.compile(r'^[0-9]+$')          # numeric NEMO ids (user/account/project)
SAFE_MACHINE_PATTERN = re.compile(r'^[a-zA-Z0-9_]+$')  # machine_id, from config allowlist

def sanitize_numeric_id(value, field_name: str) -> int:
    """Raises ValueError unless value is a positive integer (or numeric string)."""
    s = str(value)
    if not SAFE_ID_PATTERN.match(s):
        raise ValueError(f"Invalid characters in {field_name}: '{value}'")
    return int(s)

def sanitize_machine_id(value: str) -> str:
    """Raises ValueError if value is empty, contains anything outside
    [a-zA-Z0-9_], or is not present in the configured machine_id allowlist."""
    if not value or not SAFE_MACHINE_PATTERN.match(value):
        raise ValueError(f"Invalid characters in machine_id: '{value}'")
    return value
```

Call `sanitize_numeric_id()` on `user_id` and any account/project ids, and `sanitize_machine_id()` on `machine_id`, at the top of every route handler. If any field fails, return HTTP 400 immediately. Never pass raw payload values to `MountManager`, `UserProvisioner`, or any module that constructs filesystem paths or shell commands. The `sanitize_name()` function (Section 5) is a *separate* function used only for display-layer bind-mount names — it is never used to validate or construct a security-relevant path.

### modules/user_provisioner.py
```python
class UserProvisioner:
    def provision(self, user_id: int, full_name: str) -> bool:
        # user_id is NEMO's numeric user id, used directly as the Linux uid.
        # 1. useradd -r -u {user_id} -s /usr/sbin/nologin u{user_id}
        # 2. mkdir /srv/labdata/users/{user_id}
        # 3. chown {user_id}:{user_id} ...
        # 4. chmod 700 ...
        # 5. Set default ACL — any file written into this directory by any
        #    machine account will automatically inherit an ACL granting the
        #    user full access. Fixes the file ownership mismatch bug where
        #    microscope1_machine-owned files would be unreadable by the user via Nextcloud.
        #    setfacl -d -m u:{user_id}:rwx /srv/labdata/users/{user_id}/
        # 6. setquota -u {user_id} {soft} {hard} 0 0 /srv/labdata
        # 7. return True on success
        #
        # NOTE: project/group assignment is intentionally NOT done here.
        # NemoSync reconciles Linux group membership (usermod -aG) separately.
```

**Edge cases to handle:**
- User already exists in Linux (idempotent — do not fail)
- Directory already exists (idempotent — do not recreate, but re-apply default ACL)

### modules/nemo_sync.py
```python
class NemoSync:
    """
    Periodically polls the NEMO API to keep local account/project/membership
    state in sync, independent of the user_created/tool_login/tool_logout
    event stream.
    """

    def __init__(self, nemo_api_client, state_db):
        ...

    def run_once(self) -> None:
        # 1. Fetch all NEMO accounts and projects, keyed by their numeric `id`.
        # 2. For each (account, project) pair:
        #    - New project_id: upsert into `accounts`/`projects`, create
        #      /srv/labdata/groups/account_{account_id}/ (chmod 0711, root:root,
        #      if not already present) and
        #      /srv/labdata/groups/account_{account_id}/project_{project_id}/
        #      (chmod 2770, SGID, chown root:proj_{project_id}); create the
        #      Linux group proj_{project_id} if it doesn't exist.
        #    - Existing project_id whose name (or its account's name) changed:
        #      update the `name` column in `accounts`/`projects` only. The
        #      physical path (account_{id}/project_{id}) never changes — only
        #      the sanitized display name used for the NEXT tool_login's bind
        #      mount, and the Nextcloud label (Section 7), are affected.
        # 3. Fetch the full list of NEMO users and compare against the local
        #    `users` (i.e. provisioned Linux accounts) table:
        #    - New NEMO user with no local Linux account yet: call
        #      UserProvisioner.provision() (one-time backfill case, Section 19 #9)
        #    - Deactivated/deleted/reactivated accounts, projects, or users:
        #      apply the configured `nemo_sync.on_deactivation` policy
        #      (Section 12) — e.g. lock the Linux account, leave data in place,
        #      or remove group membership only
        # 4. For each user, fetch current project memberships from NEMO
        # 5. Diff against the local `memberships` table:
        #    - New membership: usermod -aG proj_{project_id} u{user_id}, insert row
        #    - Removed membership: gpasswd -d u{user_id} proj_{project_id}, delete row
        # 6. Log all changes via AuditLogger
```

**Notes:**
- Runs on a fixed interval, configurable via `nemo_sync.poll_interval_seconds` (Section 12). The institution's stated expectation is that account/project/membership changes happen only a couple of times a week, so **hourly polling is an adequate default** — this is intentionally much less frequent than the daemon's real-time event handling for `tool_login`/`tool_logout`.
- Active sessions are not disrupted by a `NemoSync` run — membership changes (including renames) affect the **next** `tool_login`, not currently mounted sessions. Because physical paths are ID-based, a rename never requires moving directories or remounting anything.
- **First run / existing users:** the first `run_once()` after initial deployment will see every NEMO account/project/user as "new" relative to an empty local state — this is the one-time backfill described in Section 19, item 9. Subsequent runs only react to incremental changes.
- **Deactivation/reactivation:** NEMO accounts, projects, and users can be deactivated, deleted, or reactivated. The behavior for each case is configurable via `nemo_sync.on_deactivation` (Section 12) so different institutions can choose, e.g., whether a deactivated user's Linux account is locked vs. left alone, and whether their data/group membership is preserved for a possible reactivation.

### modules/session_manager.py
```python
class SessionManager:
    def __init__(self):
        self._sessions: dict[int, list[str]] = {}  # {user_id: [machine_id, ...]}
        self._lock = threading.Lock()

    def add(self, user_id: int, machine_id: str) -> None
    def remove(self, user_id: int, machine_id: str) -> None
    def get_machines(self, user_id: int) -> list[str]
    def get_user(self, machine_id: str) -> int | None
    def all_sessions(self) -> dict
```

**Critical:** The session dict is accessed from multiple threads (concurrent HTTP requests). All reads and writes must acquire `self._lock`.

### modules/mount_manager.py
```python
class MountManager:
    def mount(self, user_id: int, machine_id: str,
              projects: list[dict]) -> bool:
        # projects: [{"account_id": int, "project_id": int,
        #             "account_name": str, "project_name": str}, ...]
        # 1. Run bind mounts into /mnt/labsessions/{machine_id}/
        #    mount --bind /srv/labdata/users/{user_id}/    .../my_files/
        #    for each p in projects:
        #      bind_name = f"lab_shared_{sanitize_name(p.account_name)}_{sanitize_name(p.project_name)}"
        #      mount --bind /srv/labdata/groups/account_{p.account_id}/project_{p.project_id}/  .../{bind_name}/
        #    mount --bind /srv/labdata/public/             .../public/
        # 2. Grant POSIX ACLs on source directories to machine account:
        #    setfacl -m u:{machine_account}:rwx /srv/labdata/users/{user_id}/
        #    for each p in projects:
        #      setfacl -m u:{machine_account}:r-x /srv/labdata/groups/account_{p.account_id}/project_{p.project_id}/
        #    setfacl -m u:{machine_account}:r-x /srv/labdata/public/
        # 3. Return True on success

    def unmount(self, user_id: int, machine_id: str, projects: list[dict],
                remaining_sessions: list[str]) -> bool:
        # 1. Unmount all bind mounts from /mnt/labsessions/{machine_id}/
        #    umount .../my_files
        #    for each p in projects: umount .../lab_shared_{sanitized_account}_{sanitized_project}
        #    umount .../public
        # 2. Strip POSIX ACLs for this machine account:
        #    setfacl -x u:{machine_account} /srv/labdata/users/{user_id}/
        # 3. Only strip per-project/public ACLs if no other machines are still in
        #    session for this user (check remaining_sessions via SessionManager)
        # 4. Remove now-empty session mount-point directories so they no longer
        #    appear in Windows Explorer / SMB clients after logout
        # 5. Return True on success
```

**Edge cases:**
- Bind mount target directory does not exist (create it before mounting)
- Source directory does not exist — user was never provisioned (return error, log warning)
- Mount point already has an active bind mount — previous session was not cleaned up (force unmount first with `umount -l`, log warning, then proceed)
- `umount` returns `target is busy` — open write handles still present (`IdleMonitor` handles this upstream before calling unmount)

### modules/samba_controller.py
```python
class SambaController:
    def get_open_handles(self, machine_id: str) -> list[dict]:
        # Parses smbstatus output to find open files on this share.
        # Returns a list of dicts: {"path": str, "mode": "read" | "write"}
        # so IdleMonitor can distinguish files that are merely open from
        # files that are actively being written to.

    def get_connected_clients(self, machine_id: str) -> list[str]:
        # Returns list of currently connected client IPs on this share
        # Used for audit logging and debug endpoint
```

> **Note:** `smbcontrol smbd reload-config` is not called during mount or unmount operations. Bind mounts are native kernel-level directory projections; Samba sees them as regular directories without needing to be notified. The `SambaController` is used only for inspecting Samba state, not for driving it.

### modules/idle_monitor.py
```python
class IdleMonitor:
    def wait_and_unmount(self, user_id: int, machine_id: str,
                         project_ids: list[int]) -> bool:
        # Runs in a background thread — does not block the HTTP response
        # 1. Poll samba_controller.get_open_handles() every CHECK_INTERVAL seconds
        # 2. If any handle has mode == "write", keep waiting indefinitely —
        #    never force-unmount a directory with an active write in progress
        # 3. For handles with mode == "read" only, once
        #    max_idle_timeout_seconds has elapsed, force unmount regardless
        #    and log a warning
        # 4. GHOST SESSION CHECK — re-query StateDB immediately before unmounting.
        #    If session status has changed from 'unmounting' back to 'active',
        #    the user re-logged into the same tool during the wait window.
        #    Silently abort: do NOT unmount, do NOT strip ACLs, log the abort and return.
        # 5. Look up project rows (incl. account/project names) for project_ids,
        #    then call mount_manager.unmount(user_id, machine_id, projects,
        #       session_manager.get_machines(user_id))
        # 6. Call session_manager.remove(user_id, machine_id)
        # 7. Mark session 'closed' in SQLite state DB
        # 8. Log the completed unmount
```

**Critical:** The daemon must write session state to SQLite with status `unmounting` BEFORE launching this background thread. If the daemon crashes during the wait window, the startup routine will find the `unmounting` session in SQLite and complete the unmount on restart. If the user re-logs in during the wait window, `StateDB.open_session()` will update the status back to `active`, and the ghost session check in step 4 will catch this and abort cleanly.

### modules/state_db.py
```python
class StateDB:
    """
    SQLite-backed session, group, and membership persistence.
    Ensures sessions survive daemon crashes and server reboots.
    Database path: /var/lib/labfiles/sessions.db
    """

    def __init__(self, db_path: str):
        # THREADING: check_same_thread=False is required because Flask handles
        # each HTTP request in a separate thread, and IdleMonitor / NemoSync
        # run their own background threads. Without this flag, sqlite3 raises
        # ProgrammingError on any cross-thread access.
        # WAL MODE: 'PRAGMA journal_mode=WAL' (Write-Ahead Logging) allows
        # multiple concurrent readers alongside a single writer without locking
        # the entire database.
        # BUSY TIMEOUT: 'PRAGMA busy_timeout=5000' so that a writer waiting on
        # another writer retries for up to 5 seconds instead of immediately
        # raising 'database is locked'.
        # WRITE LOCK: even with WAL, all writes must be serialized through a
        # threading.Lock to prevent lost-update races at the Python layer.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._lock = threading.Lock()
        self._create_tables()

    def open_session(self, user_id: int, machine_id: str,
                     project_ids: list[int]) -> None:
        # INSERT or REPLACE with status='active', timestamps
        # Acquire self._lock before writing

    def begin_unmount(self, user_id: int, machine_id: str) -> None:
        # UPDATE status='unmounting'
        # Acquire self._lock before writing

    def close_session(self, user_id: int, machine_id: str) -> None:
        # UPDATE status='closed'
        # Acquire self._lock before writing

    def get_active_sessions(self) -> list[dict]:
        # SELECT * WHERE status IN ('active', 'unmounting')
        # Called at daemon startup to recover orphaned sessions

    def get_session(self, user_id: int,
                    machine_id: str) -> dict | None:
        # SELECT single session record
        # Called by IdleMonitor ghost session check before unmounting

    # --- account / project / membership tables, maintained by NemoSync ---
    def upsert_account(self, account_id: int, name: str) -> None: ...
    def upsert_project(self, project_id: int, account_id: int, name: str,
                       linux_group: str, path: str) -> None: ...
    def get_projects(self) -> list[dict]: ...
    def set_memberships(self, user_id: int, project_ids: list[int]) -> None: ...
    def get_memberships(self, user_id: int) -> list[dict]:
        # Returns project rows (joined with their account) for this user,
        # i.e. [{"account_id", "project_id", "account_name", "project_name"}, ...]
        ...
```

**Daemon startup routine (in main.py):**
```python
def recover_orphaned_sessions(state_db, mount_manager, session_manager):
    """Run at startup before accepting requests."""
    orphans = state_db.get_active_sessions()
    for session in orphans:
        logger.warning(f"Recovering orphaned session: {session}")
        projects = [p for p in state_db.get_projects()
                    if p['project_id'] in session['project_ids']]
        mount_manager.unmount(
            session['user_id'], session['machine_id'],
            projects, remaining_sessions=[]
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

### How Users Sign Into NemoCE (background)
NemoCE login itself is handled via **Shibboleth/SAML SSO** (and, for walk-up kiosks, badge readers tied to a special kiosk web page). This project's plugin does not need to handle or care about NemoCE login — it only reacts to the `user_created`, `tool_login`, and `tool_logout` events NemoCE already fires once a user is authenticated. For local development/demo, local NemoCE accounts (not SSO) are sufficient.

### How NemoCE Controls Tool Access (background)
NemoCE does not connect to or control the tool PC directly. Instead, tool login/logout in NemoCE sends a command to a **Raritan smart power switch** for that bay, which powers on/off an outlet — most often the tool PC's monitor (the PC itself stays on and auto-logs-in or has credentials posted on the machine), occasionally a DC supply driving a relay/interlock for tools with no data to copy. This project's daemon is independent of that interlock: it only needs to ensure that once NemoCE reports a `tool_login`, the correct bind-mounted directories are visible over the SMB share that tool PC's saved machine credentials connect to. On the DEV NemoCE server, fake tools can be created and toggled without needing real Raritan hardware, which is the intended way to test this plugin end-to-end (Section 17).

### What a NemoCE Plugin Is
NemoCE has a Python plugin system. Plugins are Python classes that hook into Nemo's internal event system. When Nemo fires an event, it calls the corresponding method on any registered plugin.

Plugins live in Nemo's plugin directory and are loaded at startup.

### Plugin Structure
```python
# plugin/labfiles_plugin.py

class LabFilesPlugin:
    """
    NemoCE plugin for lab file access system.
    Sends HTTPS events to the file server daemon on user lifecycle events.
    """

    def on_user_created(self, user_id: str, user_data: dict) -> None:
        # Fires when new user added to Nemo
        # POST to /provision

    def on_tool_login(self, user_id: str, tool_id: str, session_id: str) -> None:
        # Fires when user selects and logs into a tool
        # Map tool_id to machine_id via config
        # POST to /mount

    def on_tool_logout(self, user_id: str, tool_id: str, session_id: str) -> None:
        # Fires when user releases a tool
        # POST to /unmount
```

> **Important:** The exact method signatures depend on the NemoCE plugin API. Review the plugin examples in the NemoCE repository before implementing, and confirm event hook names and payload formats against the version of NemoCE you are deploying against (the institution's instances currently run **NemoCE 7.4.17** with a PostgreSQL backend).
>
> **Reference implementation:** Start from the [Cookiecutter NEMO plugin template](https://gitlab.com/nemo-community/atlantis-labs/cookiecutter-nemo-plugin), which scaffolds a working plugin skeleton, and review other plugins in the NEMO Community GitLab group for examples of event hooks and payload shapes.

### HTTP Client
```python
# plugin/http_client.py

import requests

class DaemonClient:
    def __init__(self, base_url: str, api_key: str, client_cert: tuple[str, str], ca_cert: str):
        # base_url must be an https:// URL
        # client_cert: (path to client cert, path to client key) — used for mTLS
        # ca_cert: path to CA bundle used to verify the daemon's server certificate
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers['X-API-Key'] = api_key
        self.session.cert = client_cert
        self.session.verify = ca_cert
        self.timeout = 10

    def provision(self, user_id, full_name) -> bool: ...
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
   workgroup = WORKGROUP
   server string = PRISM Lab File Server
   security = user
   map to guest = never
   log file = /var/log/samba/log.%m
   max log size = 50

   # Bind smbd only to the VLAN-facing interface.
   # The internet-facing interface (used by the daemon API and Nextcloud)
   # should NOT have smbd listening on it at all.
   interfaces = eth1
   bind interfaces only = yes

   # Restrict which hosts/subnets may connect to Samba at all.
   hosts allow = 10.10.0.0/24
   hosts deny = 0.0.0.0/0

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
```

> **Key design notes:**
> - Share paths point to `/mnt/labsessions/{machine}/`, not to `/srv/labdata/`. This is the session mount directory, not the data directory.
> - `wide links` and `follow symlinks` are deliberately absent. Bind mounts are native directories to Samba; no special flags are needed, and removing them closes a known security hole.
> - There is **no `[userdata]` / `[homes]` share and no VPN-based laptop access**. Off-site/personal access to `/srv/labdata/users/<user_id>/` and group directories is provided exclusively through **Nextcloud** (Section 4), which is configured separately (see `docs/nextcloud_setup.md` — to be written) to point its External Storage mounts at the same paths.
> - `smbd` is bound to a single VLAN-facing interface via `interfaces` / `bind interfaces only`, and `hosts allow`/`hosts deny` further restrict connections to the tool PC subnet. See [Section 18](#18-security-architecture).

---

## 11. Linux System Setup

### OS / Distro Approach

The host OS is **not prescribed** by this project. The institution's IT department prefers **RHEL** for the file server (so they can manage OS security updates and patching), but is open to other distros if the daemon and Samba are containerized via **Docker** — Docker lets the same image run identically on RHEL, Ubuntu, or any other distro IT chooses, while IT continues to patch the underlying host OS independently of this project's code.

This project therefore targets a **Docker-based deployment**:
- The daemon (and, if convenient, `smbd`) run in containers built from a project-provided Dockerfile/`docker-compose.yml` (mirroring the pattern already used for the NemoCE dev container in `nemo_server/`).
- Host directories (`/srv/labdata`, `/mnt/labsessions`, `/var/lib/labfiles`, `/var/log/labfiles`) are bind-mounted into the container(s), so data persists independent of the container lifecycle.
- Operations that require host kernel features — `mount --bind`, `setfacl`, disk quotas via `setquota`, `useradd`/`usermod` for Linux/Samba accounts — require the container to run with elevated privileges (`--privileged` or a curated `--cap-add` set including at least `CAP_SYS_ADMIN` for bind mounts) and `propagation=shared` / `rshared` mount propagation so bind mounts made inside the container are visible to `smbd` on the host (or in a sibling container) and vice versa. This needs to be validated during Phase 6/8 testing (Section 16) and documented in `docs/deployment.md`.
- The steps below describe what must exist on the **host filesystem** regardless of whether `smbd`/the daemon run in containers or natively; a Dockerfile automates package installation inside the image, but the host-side directory structure, ACL-enabled mount, and quota setup are host (not container) concerns either way.

Run once on the file server (host) to initialize the environment.

### Required Packages (inside the daemon/Samba container image)
```dockerfile
# Example, adapted to whichever base image IT standardizes on (RHEL UBI, Ubuntu, etc.)
# RHEL UBI / dnf-based:
RUN dnf install -y samba samba-common-tools quota acl python3 python3-pip

# Debian/Ubuntu-based, for local dev:
# RUN apt update && apt install -y samba samba-common-bin quota acl python3 python3-pip python3-venv
```

(Nextcloud is installed separately per its own documentation; this project only requires that Nextcloud's External Storage app — with the `user_saml` app enabled, Section 4 — can read/write the same `/srv/labdata/users/<user_id>/` and `/srv/labdata/groups/account_<account_id>/project_<project_id>/` paths that Samba serves.)

### Enable ACL Support on /srv/labdata (host)

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
sudo mkdir -p /mnt/labsessions/microscope1/{my_files,public}
sudo mkdir -p /mnt/labsessions/microscope2/{my_files,public}
# lab_shared_<sanitized_account>_<sanitized_project> directories are created
# on-demand by MountManager the first time a user with that project
# membership logs in.

# SQLite state database directory
sudo mkdir -p /var/lib/labfiles
```

### Linux Groups (one per project)

Account directories, project directories, and per-project Linux groups are created automatically by `NemoSync` on its first run. To create one manually (e.g., for initial testing) — account 42 ("Woo Lab"), project 10 ("Quantum Sensing"):

```bash
sudo groupadd proj_10

sudo mkdir -p /srv/labdata/groups/account_42
sudo chown root:root /srv/labdata/groups/account_42
sudo chmod 0711 /srv/labdata/groups/account_42   # traversal-only, no listing

sudo mkdir -p /srv/labdata/groups/account_42/project_10
sudo chown root:proj_10 /srv/labdata/groups/account_42/project_10
sudo chmod 2770 /srv/labdata/groups/account_42/project_10   # 2 = SGID bit
sudo setfacl -d -m g:proj_10:rwx /srv/labdata/groups/account_42/project_10
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

> **Note:** The commands above (`mkdir`, `groupadd`, `useradd`, `smbpasswd`, `quotacheck`/`quotaon`, `/etc/fstab` edits) are written as host commands but apply equally inside a privileged container with the host filesystem bind-mounted in — the operations and paths are identical either way. What changes under the Docker approach is only *where* `samba`/`acl`/`quota`/`python3` packages are installed (in the container image, via the Dockerfile above, vs. directly on the host OS).

---

## 12. Config File Format

### config/config.yaml
```yaml
server:
  host: "127.0.0.1"        # bind to a single interface, not 0.0.0.0
  port: 5443
  api_key_hash: "CHANGE_THIS_IN_PRODUCTION"   # store a hash, not the raw key
  tls:
    cert_file: "/etc/labfiles/tls/server.crt"
    key_file: "/etc/labfiles/tls/server.key"
    client_ca_file: "/etc/labfiles/tls/client_ca.crt"   # for mutual TLS
    require_client_cert: true

rate_limiting:
  default: "60/minute"
  mount_endpoints: "20/minute"

storage:
  base_path: "/srv/labdata"
  quota_soft_mb: 10240     # 10 GB
  quota_hard_mb: 12288     # 12 GB hard limit

sessions:
  mount_base_path: "/mnt/labsessions"   # where bind mounts are created
  db_path: "/var/lib/labfiles/sessions.db"  # SQLite state database

samba:
  status_command: "smbstatus"

idle_monitor:
  check_interval_seconds: 5
  max_idle_timeout_seconds: 30   # ceiling for read-only open handles only;
                                  # active write handles are never force-unmounted

nemo_sync:
  api_base_url: "https://nemo.example.edu/api/"
  api_token: "CHANGE_THIS_IN_PRODUCTION"
  poll_interval_seconds: 3600     # hourly default; NEMO account/project/membership
                                   # changes are infrequent (a few per week)
  on_deactivation: "lock_account" # "lock_account" | "remove_membership_only" | "ignore"
                                   # policy applied when a NEMO account/project/user
                                   # is deactivated, deleted, or reactivated

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
```

---

## 13. Systemd Unit File

### systemd/labfiles-daemon.service
```ini
[Unit]
Description=Lab File Access Daemon
Documentation=https://github.com/your-org/labfiles
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

# --- Sandboxing ---
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/labdata /mnt/labsessions /var/lib/labfiles /var/log/labfiles
PrivateTmp=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictNamespaces=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
```

> **Why `User=root`:** the daemon must call `mount`, `umount`, `setfacl`, `useradd`, and `usermod`, all of which require root. The systemd sandboxing directives above (`ProtectSystem=strict`, explicit `ReadWritePaths`, `NoNewPrivileges`, etc.) constrain what a compromised daemon process can do even while running as root — see [Section 18.5](#18-security-architecture).

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
│   ├── http_client.py             # HTTPS (mTLS) POST to daemon
│   └── plugin_config.py           # Plugin-specific config loader
│
├── daemon/                        # File server daemon (runs on file server)
│   ├── __init__.py
│   ├── main.py                    # Entry point, starts Flask/FastAPI over TLS
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py              # POST /provision, /mount, /unmount
│   │   └── auth.py                # API key + rate limit middleware
│   └── modules/
│       ├── __init__.py
│       ├── user_provisioner.py
│       ├── session_manager.py
│       ├── mount_manager.py
│       ├── samba_controller.py
│       ├── idle_monitor.py
│       ├── nemo_sync.py
│       ├── state_db.py            # SQLite session/group/membership persistence
│       └── audit_logger.py
│
├── config/
│   ├── config.yaml                # Active config (gitignored)
│   └── config.example.yaml        # Template, checked into repo
│
├── systemd/
│   └── labfiles-daemon.service    # systemd unit file (for non-containerized hosts)
│
├── docker/
│   ├── Dockerfile                 # Daemon image (distro-agnostic, Section 11)
│   └── docker-compose.yml         # Daemon (+ optionally smbd) container(s),
│                                   # with /srv/labdata, /mnt/labsessions,
│                                   # /var/lib/labfiles, /var/log/labfiles bind-mounted
│
├── samba/
│   └── smb.conf.template          # Samba config template with placeholders
│
├── scripts/
│   ├── setup_filesystem.sh        # Creates /srv/labdata structure
│   ├── setup_machine_accounts.sh  # Creates Samba machine accounts
│   └── setup_quotas.sh            # Enables quota on the partition
│
├── tests/
│   ├── test_user_provisioner.py
│   ├── test_mount_manager.py
│   ├── test_session_manager.py
│   ├── test_samba_controller.py
│   ├── test_idle_monitor.py
│   ├── test_nemo_sync.py
│   └── test_integration.py        # End-to-end: full login/logout cycle
│
├── docs/
│   ├── system_design.md           # System design paper (for research publication)
│   ├── deployment.md              # How to deploy at a new institution
│   ├── nextcloud_setup.md         # How to wire Nextcloud External Storage to /srv/labdata
│   └── nemo_plugin_notes.md       # Notes on NemoCE plugin API
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
flask-limiter>=3.5.0   # rate limiting
pyyaml>=6.0
python-dotenv>=1.0
requests>=2.31.0       # used by NemoSync to call the NEMO API
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

This is a living checklist tracking implementation progress across phases. Each phase builds on the previous one and is intended to be testable end-to-end before moving on.

---

### Phase 1 — Foundation
- [x] Set up Git repository with the full directory structure from Section 14
- [x] Get a local NemoCE instance running via Docker
- [x] Read the NemoCE plugin documentation and review existing plugin examples
- [x] Write `daemon/main.py` — Flask app skeleton
- [x] Write `daemon/api/routes.py` — POST /provision, /mount, /unmount
- [x] Write `daemon/api/auth.py` — API key middleware
- [x] Write `daemon/modules/audit_logger.py` — structured logging from day one

---

### Phase 2 — Provisioning
- [x] Write `daemon/modules/user_provisioner.py`
- [x] Implement: Linux user creation, directory creation, chmod/chown, default ACL
- [x] Write `config/config.example.yaml` with full schema
- [x] Write `scripts/setup_filesystem.sh`

---

### Phase 3 — Session Management & Mounting
- [x] Write `daemon/modules/session_manager.py` with thread safety
- [x] Write `daemon/modules/state_db.py` — SQLite session persistence (WAL + busy_timeout)
- [x] Write `daemon/modules/mount_manager.py` — bind mounts, ACL grants/strips, cleanup of empty mount-point dirs on unmount
- [x] Write `daemon/modules/samba_controller.py` — `smbstatus` parsing
- [x] Full login/logout cycle tested end-to-end against real NemoCE: bind mounts and session folders appear and disappear correctly in Windows Explorer

---

### Phase 4 — Graceful Disconnect & Recovery
- [x] Write `daemon/modules/idle_monitor.py`
- [x] Implement daemon startup orphan recovery (recover_orphaned_sessions)
- [ ] Update `get_open_handles()` to distinguish read vs write modes, and update `IdleMonitor` to never force-unmount active writes (v4 behavior)

---

### Phase 5 — Multi-Project Mounts & NemoSync
- [ ] Write `daemon/modules/nemo_sync.py` — poll NEMO API, maintain `accounts`/`projects`/`memberships` tables
- [ ] Implement per-project Linux group (`proj_<project_id>`) and `account_<id>/project_<id>/` directory creation/sync
- [ ] Implement `sanitize_name()` (Section 5) for bind-mount and Nextcloud display names
- [ ] Update `MountManager` to mount one `lab_shared_<sanitized_account>_<sanitized_project>` per project membership instead of a single `lab_shared`
- [ ] Update `tool_login`/`tool_logout` handlers to use `project_ids: list[int]` (with account/project name lookups for display names) instead of `group_id: str`
- [ ] Write `tests/test_nemo_sync.py`

---

### Phase 6 — Security Hardening
- [ ] Move daemon to HTTPS-only, bound to a single interface (Section 18.1–18.2)
- [ ] Implement mutual TLS between plugin and daemon (Section 18.3)
- [ ] Add `flask-limiter` rate limiting and machine_id allowlist (Section 18.4)
- [ ] Add systemd sandboxing directives to the unit file (Section 18.5)
- [ ] Harden `smb.conf`: `interfaces`/`bind interfaces only`, `hosts allow`/`hosts deny` (Section 18.6)
- [ ] Switch `api_key` storage to a hash, not plaintext, in config
- [ ] Write `docker/Dockerfile` and `docker/docker-compose.yml` for the daemon (Section 11/14); validate that `mount --bind`, `setfacl`, and quota commands work from inside the container against host-mounted `/srv/labdata` and `/mnt/labsessions`, with bind mounts visible to `smbd`

---

### Phase 7 — Off-Site Access via Nextcloud + SSO
- [ ] Stand up Nextcloud instance on/alongside the file server
- [ ] Configure Nextcloud External Storage (local) to point at `/srv/labdata/users/<user_id>/` per user
- [ ] Configure Nextcloud External Storage (group folders) to point at `/srv/labdata/groups/account_<account_id>/project_<project_id>/`, one per project
- [ ] Configure Nextcloud's `user_saml` app against the institution's Shibboleth/SAML IdP, and verify both NetID and Guest accounts can log in
- [ ] Verify file ownership/ACL behavior: a file created via Nextcloud is correctly readable by the tool PC machine account, and vice versa
- [ ] Write `docs/nextcloud_setup.md`

---

### Phase 8 — Integration, Testing & Documentation
- [ ] Full end-to-end integration test: NemoCE → Plugin → Daemon → Samba → file visible on tool PC, and same file visible via Nextcloud
- [ ] Error handling pass: what happens if Samba is down? If the base directory is missing? If provisioning fails halfway? If NemoSync's API call fails?
- [ ] Review all subprocess calls for security (no `shell=True` with user input)
- [ ] Write `docs/deployment.md` — step-by-step for a new institution to deploy this
- [ ] Write `docs/system_design.md`

---

## 17. Testing Plan

### Unit Tests (pytest)
Each module gets its own test file. Filesystem operations are tested against a temporary directory (use `tmp_path` in pytest). Subprocess calls to `mount`, `umount`, `setfacl`, `useradd`, etc. are mocked.

### Integration Tests
`test_integration.py` spins up the Flask daemon in a test process, fires HTTP POST requests simulating Nemo events, and verifies filesystem state and SQLite state at each step.

### Manual Testing Checklist
Before a demo or release, manually verify each scenario:
- [ ] New user provisioned: directory created, correct permissions, quota set, default ACL present (`getfacl /srv/labdata/users/709` shows default entry)
- [ ] File written by machine account is readable by user via Nextcloud: create a file as `microscope1_machine` inside `users/709/`, confirm user 709 can read it through Nextcloud
- [ ] File written via Nextcloud is readable by the machine account on next tool login
- [ ] User logs into one tool: bind mounts active for `my_files`, each `lab_shared_<sanitized_account>_<sanitized_project>` for a project the user belongs to, and `public`; POSIX ACLs granted; files visible via SMB
- [ ] User logs into two tools simultaneously: bind mounts in both session dirs, both machine accounts have ACL entries
- [ ] User logs out of one tool: that machine's mounts unmounted and ACLs stripped, other session unaffected; session folders disappear from Explorer
- [ ] User logs out with a file open for **reading only**: system waits up to `max_idle_timeout_seconds`, then force-unmounts
- [ ] User logs out with a file open for **writing**: system waits indefinitely and does not unmount until the write handle closes
- [ ] Ghost session: user logs out, immediately logs back in before the idle timeout — confirm files remain accessible and no unmount occurs
- [ ] Daemon crash mid-session: restart daemon, confirm orphan recovery runs and mounts are cleaned up
- [ ] Path traversal attempt: send `user_id: "../../../etc"` to `/mount`, confirm 400 returned (fails numeric-id validation)
- [ ] User in account 42 but only on project 10 cannot access project 11's data: confirm `ls /srv/labdata/groups/account_42/` as that user shows nothing, and no `lab_shared_..._project_11` mount appears at login
- [ ] NemoSync adds a new project membership: next `tool_login` for that user includes a new `lab_shared_<sanitized_account>_<sanitized_project>` mount
- [ ] NemoSync removes a project membership: next `tool_login` for that user no longer includes that mount, but an already-active session is unaffected
- [ ] NemoSync detects a renamed account or project: physical path `account_<id>/project_<id>/` is unchanged; next `tool_login` mounts under the new sanitized bind-mount name
- [ ] mTLS: a request without a valid client certificate is rejected
- [ ] Rate limiting: rapid repeated requests from one source receive 429
- [ ] Provisioning called twice (idempotent): no errors, default ACL still correct

---

## 18. Security Architecture

This section describes the defense-in-depth layers protecting the daemon and the data it manages. Each layer assumes the others may fail, so no single misconfiguration should fully expose the system.

### 18.1 Interface Binding

The daemon's HTTPS API binds to a single, specific interface/address — never `0.0.0.0`. If the file server is dual-homed (one interface on the VLAN, one on the internet/campus network), the daemon should bind only to the interface reachable by the Nemo server. Similarly, `smbd` is configured with `interfaces` + `bind interfaces only = yes` so it only listens on the VLAN-facing interface (Section 10).

### 18.2 HTTPS / TLS

The daemon's API is HTTPS-only. Plain HTTP is not supported, even on a "trusted" network — credentials (`X-API-Key`) and user identifiers should never be sent in cleartext. TLS certificates can be self-signed/internal-CA-issued, since both endpoints (Nemo plugin and daemon) are under the same administrative control.

### 18.3 Mutual TLS (mTLS)

In addition to the daemon presenting a TLS certificate to the plugin, the plugin presents a **client certificate** to the daemon. The daemon verifies this client certificate against a configured CA before processing any request. This ensures that even if the API key were leaked, an attacker without the corresponding client certificate/key cannot call the daemon's API.

### 18.4 Application-Layer Protections

- **API key**: stored as a hash in `config.yaml`, compared via constant-time comparison
- **Rate limiting**: `flask-limiter` enforces per-endpoint limits (Section 12) to slow brute-force or runaway-loop scenarios
- **machine_id allowlist**: `/mount` and `/unmount` requests are checked against the `machines` list in `config.yaml`; unrecognized `machine_id` values are rejected
- **Input sanitization**: `user_id` and account/project ids are validated as numeric NEMO ids, and `machine_id` against an allowlist regex and the configured `machines` list (`sanitize_numeric_id`/`sanitize_machine_id`, Section 8), before being used in filesystem paths or shell commands

### 18.5 Filesystem & Process Sandboxing

The daemon runs as `root` (required for `mount`/`setfacl`/`useradd`), but systemd sandboxing directives (Section 13) constrain it: `ProtectSystem=strict` makes most of the filesystem read-only to the process, `ReadWritePaths` explicitly lists the only writable locations, `NoNewPrivileges` prevents privilege escalation via `setuid` binaries, and `PrivateTmp`, `ProtectKernelModules`, `ProtectKernelLogs`, `ProtectControlGroups`, `RestrictNamespaces`, and `LockPersonality` reduce the kernel attack surface available even to a compromised root process.

### 18.6 Samba / Network Hardening

- `interfaces` + `bind interfaces only = yes` restrict `smbd` to the VLAN interface
- `hosts allow` / `hosts deny` restrict Samba connections to the known tool-PC subnet
- Machine account passwords (`smbpasswd`) follow the institution's standard password/lockout policy

### 18.7 SQLite Hardening

`PRAGMA journal_mode=WAL` plus `PRAGMA busy_timeout=5000` (Section 8, `state_db.py`) avoid `database is locked` failures under concurrent access from the API server, `IdleMonitor`, and `NemoSync` without requiring an external database server.

---

## 19. Open Questions

Items that need to be resolved with the facility's NemoCE administrator and IT contacts before or during implementation. Names/contacts intentionally omitted from this public document — see internal project tracker.

### Resolved

The following questions were answered directly by the facility's NemoCE administrator and are recorded here for reference:

- **`group_mapping_mode` choice (was #2):** **Superseded — see "Still Open" item 8 below.** MNFC originally answered `"account"` (NEMO Accounts, not Projects, as the sharing unit for `lab_shared_<group_id>` directories). After reviewing real NEMO data — 86% of users span multiple accounts, and users within one account are often on different subsets of that account's projects — the design has moved to a **combined model**: directories are organized by account but access control (Linux groups, ACLs) is enforced at the **project** level (Section 5, 8, 12). This needs to be confirmed with MNFC.
- **Nemo server network location (was #3):** **Resolved.** NemoCE runs in Docker on a separate, centrally-managed server (DEV + PROD instances), reachable from the campus/internal network — not on the VLAN (Section 4).
- **File server network (was #4):** **Resolved — dual-homed.** The file server has one interface on the VLAN (tool PCs, SMB only) and one on the campus network (NemoCE, daemon API, Nextcloud, admins). The daemon binds only to the campus-network interface (Section 4, 18.1).
- **Existing users (was #9):** **Resolved — backfill is needed.** NEMO can provide a full API-driven list of accounts/projects/users. `NemoSync`'s first run performs the backfill (provision + reconcile everyone), and subsequent runs react only to incremental changes (Section 8).
- **Quota sizes (was #10):** **Resolved — configurable, start small.** Quotas are a `config.yaml` option (Section 12); the facility wants to start with small demo quotas and increase them once the production server and user count are known.
- **Group assignment data source (related to was #2):** **Resolved.** NEMO's `accounts -> projects -> users` hierarchy is the source. NEMO's numeric `id` (not the name) is the stable key used for physical paths and Linux uids/gids (`users/<user_id>`, `groups/account_<account_id>/project_<project_id>`, `proj_<project_id>`); human-readable names are sanitized only for display purposes — bind-mount names and Nextcloud labels (Section 5, 6, 8).
- **Guest/shared "badge#" accounts (was #7, partially):** **Resolved — out of scope.** The `badge#` field in NEMO is used only for kiosk touchscreen login and is not relevant to this plugin.

### Still Open

1. **NEMO API access for NemoSync:** What credentials/scopes does the NEMO API expose for listing accounts, projects, and user memberships? Is there a dedicated service-account token, or does NemoSync need to use an existing admin account's credentials? (Example data feed files for accounts/projects/users have been provided and should be reviewed against the schema in Section 5/8 — see Section 20.)

2. **SMB client refresh behavior:** Some SMB clients cache directory listings aggressively. After a bind mount appears/disappears, how reliably do Windows Explorer and other tool-PC OSes pick up the change without a manual refresh or reconnect? Initial guidance suggests modern Windows clients (7+) respond to an SMB-level refresh/reset signal, but the exact mechanism (and whether Linux clients need different handling) needs to be determined experimentally (Phase 8 testing).

3. **Legacy tool PCs / SMB1:** Are there any tool PCs running an OS old enough to require SMB1? SMB1 has known security weaknesses — if any such machines exist, they may need to be isolated further or excluded from this system.

4. **Nextcloud + SAML details:** IT is still researching the best way to support SSO for a mixed population of NetID (in AD + Shibboleth) and Guest (Shibboleth only, not AD) accounts. This README specifies Nextcloud's `user_saml` app against Shibboleth as the chosen approach (Section 4), but IT's recommendation may evolve — confirm before Phase 7. Also: will Nextcloud run on the same host as the file server/daemon, or a separate host with network access to `/srv/labdata`? Does Nextcloud's "External Storage" local filesystem backend preserve POSIX ownership/ACLs correctly, or will additional reconciliation be needed?

5. **Docker privilege model for the file server:** If the daemon/Samba run in containers per Section 11, what capability set (vs. full `--privileged`) is the minimum needed for `mount --bind`, `setfacl`, `useradd`/`usermod`, and quota commands to work against host-mounted `/srv/labdata`, and is this acceptable to IT from a security standpoint given they'll be managing the underlying RHEL host?

6. **Tool-to-machine mapping file:** The facility will need to produce the actual list of tool IDs ↔ machine hostnames ↔ any per-machine share credentials for each real installation, to populate the `machines:` section of `config.yaml` (Section 12). This is facility-specific setup data, not a design question, but is listed here as a deployment prerequisite.

7. **Mounting/unmounting disturbance to running equipment:** Tool PCs are generally left on permanently with auto-login or posted credentials, and connect to their SMB share automatically. Bind mount/unmount operations on the file server should not disturb the already-established SMB session, but this should be verified on real tool PCs (Phase 8) — particularly whether any client-side reconnect or share re-browse is needed after a mount/unmount.

8. **Project-level isolation vs. MNFC's stated `group_mapping_mode: "account"` answer:** MNFC's literal answer to the group/lab identifier question was "account" — i.e., share data at the lab/PI level. Analysis of the real account/project/user export data showed that within a single account, different users are frequently on different subsets of that account's projects (Section 5, "The Data Reality"). Sharing everything at the account level would let a user see files for projects under their PI that they are not actually part of. This README now specifies **project-level Linux groups nested under account-named directories** (`account_<id>/project_<id>/`) so that access is restricted to the specific projects a user is on, while keeping the account as an organizational grouping for the user-visible folder name. **This needs to be confirmed with MNFC** — specifically, whether project-level isolation within an account is in fact desired/required, or whether account-level sharing (as originally stated) is acceptable for this facility.

---

## 20. Links & Resources

### Samba
- Samba documentation home: https://www.samba.org/samba/docs/
- smb.conf man page: https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html
- smbstatus man page: https://www.samba.org/samba/docs/current/man-html/smbstatus.1.html

### Nextcloud
- Nextcloud External Storage documentation: https://docs.nextcloud.com/server/latest/admin_manual/configuration_files/external_storage_configuration_gui.html
- Nextcloud Group Folders app: https://github.com/nextcloud/groupfolders
- Nextcloud `user_saml` app (SSO via SAML/Shibboleth): https://github.com/nextcloud/user_saml

### NemoCE Plugin Development
- Cookiecutter NEMO plugin template (Atlantis Labs GitLab): https://gitlab.com/nemo-community/atlantis-labs/cookiecutter-nemo-plugin
- NEMO Community plugin examples: browse the same GitLab group for other published plugins

### Facility-Provided Reference Data
- Example NEMO API data feed files (accounts, projects, users) provided by the facility — see internal project tracker for location. These have been reviewed (see Section 5, "The Data Reality") and informed the `accounts`/`projects`/`memberships` schema in Section 5 and the `NemoSync` spec in Section 8; confirm field names against the live API (especially numeric `id` fields) before implementing Phase 5.

### Linux System Administration
- systemd service files: https://www.freedesktop.org/software/systemd/man/systemd.service.html
- systemd sandboxing directives: https://www.freedesktop.org/software/systemd/man/systemd.exec.html
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
- Flask-Limiter documentation: https://flask-limiter.readthedocs.io/
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
- Mutual TLS explained: https://www.cloudflare.com/learning/access-management/what-is-mutual-tls/
