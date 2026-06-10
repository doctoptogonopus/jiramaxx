# Jiramaxx

A lightweight desktop GUI for creating and managing Jira tickets without leaving your keyboard. Runs as a background daemon and pops up on a global hotkey.

---

## Requirements

- Python 3.10+
- A Jira Cloud account with an API token

```
poetry install
```

### Installing

```
pip install jiramaxx              # Jira tool only
pip install jiramaxx[recording]   # also install the optional recording plugin
```

Recording lives in a **separate distribution** (`jiramaxx-recording`) so it can
be left out of a corporate package mirror where audio capture is restricted. The
`[recording]` extra is just a convenience alias that also installs that package;
`pip install jiramaxx-recording` does the same thing. There is **no separate
command** — the app is always launched with `jiramaxx`. When the recording
package is present, a Record button and a Recording tab appear automatically;
when it is absent, they simply don't.

> In a curated mirror that carries `jiramaxx` but not `jiramaxx-recording`,
> `pip install jiramaxx[recording]` fails cleanly (nothing is installed from an
> unauthorized source), while plain `pip install jiramaxx` always works.

---

## Setup

### 1. First run

```
jiramaxx
OR
jiramaxx --gui
```

On first run, a default `config.yaml` is created at **`~/.jiramaxx/config.yaml`** (your home directory — not inside the installed package). The widget requires you to fill in your credentials (see below) and run again, or use the in-widget Config screen. An empty or partially-filled config is fine — missing settings fall back to their defaults and you'll be prompted for the API token.
_If running with --gui flag, the script will stop running upon exiting rendering shortcuts unavailable._

### 2. Configure credentials

Open the Config screen (press **C** on the main window or run `python main.py --gui` and press C), go to the **Jira** tab.

| Field | What to enter |
|---|---|
| **Base URL** | Your Atlassian domain, e.g. `https://yourcompany.atlassian.net` |
| **API Token** | Generated at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| **User Email** | The email address on your Atlassian account |
| **Project Key** | The short key for your project (e.g. `ENG`, `JCSD`). Use the **Browse** button to find it. |
| **My Account ID** | Your Atlassian account ID. Found at: Jira → Profile → the ID in the URL, or via `GET /rest/api/3/myself`. Used when assigning tickets to yourself. |

> **Where the token is stored.** On Save, the API token is placed in the **OS credential store**
> (Windows Credential Manager — per-user, no admin rights needed) and `config.yaml` keeps only the
> placeholder `@keyring`. If the credential store isn't available (e.g. disabled by group policy),
> the token is kept in `config.yaml` as before.

#### Finding your Project Key

Click **Browse** next to the Project Key field. The tool will connect to Jira using the credentials currently typed in the form and display all accessible projects. Click one and press **Select** (or double-click) to fill the field automatically.

#### API Token — important notes

Use the **"Create API token with scopes"** button (not "Create API token"). This leverages OAuth 2.0 and allows you to disable unecessary permissions for the token.
**Required Scopes:**
    - `read:jira-user`
    - `read:jira-work`
    - `write:jira-work`

_Atlassian's regular tokens are unscoped and incongruent with least-privileged access design. If necessary or unconcerned 🤔 the classic token can be used by selecting "classic" for your Token Type in the Configurations UI._
_Note: Classic tokens already inherit exactly your Jira account's permission level. For least-privileged access, restrict your account's role on the project in **Jira → Project Settings → Permissions** rather than restricting the token itself._

---

### 3. Custom Field IDs

Jira stores certain fields under instance-specific IDs (e.g. `customfield_10016`). These vary per Jira instance. Find them in **Project Settings → Fields** or ask your Jira admin.

| Setting | What it controls | Common value |
|---|---|---|
| **Story Points field**  | The custom field used to store story point estimates | `customfield_10016` |
| **Epic Link field**     | The custom field that links a Story/Task to an Epic | `customfield_10014` |
| **Epic Name field**     | The custom field for an Epic's short name label | `customfield_10011` |
| **Sprint Name field**   | The custom field for the Sprints in the selected Jira Project Key  | `customfield_10020` |

Leave these blank to use the defaults above. If story points are not saving, this is the first thing to check.

---

## Running

### GUI mode (direct)

```
jiramaxx --gui
```

Opens the main window immediately. Use this for one-off ticket creation.

### Daemon mode (background hotkey listener)

```
jiramaxx
```

Runs silently in the background allowing you to open the widget on demand. Press the configured hotkeys anywhere on your system to open the GUI. **Exit widget** & press **Ctrl-C** in the terminal to quit.

---

## Main Window

```
┌─────────────────────────────────────┐
│  Jiramaxx                          │
│  2 incomplete draft(s) — press D…   │
│  ─────────────────────────────────  │
│  [ (N) New Ticket  ] [ (D) Drafts ] │
│  [ (M) Manage      ] [ (C) Config ] │
│  [        (Q) Quit               ]  │
└─────────────────────────────────────┘
```

| Button / Key | Action |
|---|---|
| **N** | Open the ticket type selector, then the ticket form |
| **D** | Open the draft list (disabled when no drafts exist) |
| **M** | Open the sprint ticket manager (Add Comment / Change Status) |
| **G** | Open the **initiative planner** (visual relationship graph) |
| **C** | Open the configuration editor |
| **Q** / Escape | Quit |

The draft counter updates automatically after every action.

---

## Creating a Ticket

1. Press **N** (or click New Ticket).
2. **Title first.** A single field asks for the ticket title. Then:
   - **Edit** (Enter) — continue to the type selector and the full form (your title is carried over, pre-filled).
   - **Save** (Ctrl+S) — quick-save the title as a **draft** (typed as a Task) to finish later from Drafts.
   - **Cancel** (Escape) — discard.
3. Select a ticket type — keyboard shortcuts **1–5** or the **first letter** of the type name (S, B, T, E, I) also work.
4. Fill in the form. Required fields are marked with `*`. Use **Tab / Shift+Tab** to move between fields (Tab does not insert whitespace in multiline fields).
5. Choose an action:

| Button / Shortcut | Action |
|---|---|
| **Submit to Jira** / Ctrl+Enter | Validates required fields, sends to Jira API, marks draft as submitted and keeps a local copy. |
| **Save Draft** / Ctrl+S | Saves locally without submitting. You can return to it later via Drafts. |
| **Cancel** / Escape | Closes without saving. |

### Field reference

| Field | Type | Notes |
|---|---|---|
| **Summary** | Text | The ticket title. Required on all types. |
| **Description** | Multiline text | Free-form description. Required on all types. |
| **Story Points** | Spinner (0,1,2,3,5,8,13,21) | Fibonacci scale. Saved to the custom field configured in Config → Custom Field IDs. |
| **Assignee (account ID)** | Text | Must be an Atlassian **account ID**, not a display name or email. Type `me` to assign to yourself (uses **My Account ID** from Config). Leave blank to leave unassigned. |
| **Labels** | Text | Comma-separated list of labels, e.g. `backend, urgent`. |
| **Sprint** | Dropdown | Populated from the sprint cache. Use **Refresh Sprints** in Config → Jira to populate. Select `(Backlog)` to leave unassigned to a sprint. |
| **Epic Link** | Text | The Jira issue key of an existing Epic to link to, e.g. `JCSD-12`. Full browse URLs are also accepted and stripped down to the key automatically. Sent as the Epic Link custom field. |
| **Epic Name** | Text | Short label for the Epic itself. Only meaningful on Epic tickets. |
| **Severity** | Dropdown | Critical / High / Medium / Low. Bug tickets only. |
| **Steps to Reproduce** | Multiline text | Bug tickets only. |
| **Priority** | Dropdown | Highest / High / Medium / Low / Lowest. |

### Ticket types and their default fields

| Type | Default Required | Default Optional |
|---|---|---|
| **Story** | Summary, Story Points, Description | Assignee, Labels, Sprint, Epic Link, Priority |
| **Bug** | Summary, Description, Severity, Steps to Reproduce | Assignee, Labels, Priority |
| **Task** | Summary, Description | Assignee, Story Points, Labels, Priority |
| **Epic** | Summary, Description, Epic Name | Labels, Priority |
| **Initiative** | Summary, Description | Labels, Priority |

Which fields are required vs. optional is fully configurable per ticket type in **Config → Ticket Types**.

---

## Drafts

Drafts are stored as YAML files under your **data folder** — `~/.jiramaxx/drafts/` by default (configurable via **Data folder**, see Widget Settings). A draft is created any time you click **Save Draft** (or quick-save a title from the New Ticket screen). After a successful Jira submission the ticket is also retained locally (marked submitted) so you have a local record.

The list is ordered **newest-first** by default. Click the **☰** button in the top-right corner to sort by creation time (newest/oldest), type, or summary.

### Draft list actions

| Button / Key | Action |
|---|---|
| **Open** / Enter / double-click | Re-open the ticket form to continue editing or submit |
| **Delete** | Permanently removes the local draft (prompts for confirmation) |
| **☰** (top-right) | Change the sort order (no hotkey) |
| **Cancel** / Escape | Returns to the main window |

---

## Managing Existing Tickets

Press **M** to open the sprint ticket manager. It shows the issues in your **active sprint** that are **assigned to you**.

The first ticket is selected automatically — use the **↑ / ↓ arrow keys** to navigate the list without clicking.

The list shows columns for **Key, Summary, Epic, and Due date**. (In the grouped view, Status and Due appear as columns alongside the epic tree.)

**It opens instantly from a local snapshot** — the sprint list is cached on disk (a single overwritten file under `<data folder>/active_tickets/`) so re-opening the manager does not hit the network. Press **U** (**Update**) to refetch the current sprint from Jira and refresh the snapshot.

| Button / Key | Action |
|---|---|
| **C** | Add a comment to the selected ticket |
| **S** | Change the status of the selected ticket |
| **T** | Create a **subtask** of the selected ticket |
| **U** | **Update** — refetch the sprint from Jira |
| **☰** (top-right) | Sort the list / group by epic (see below; no hotkey) |
| **Release** (top-right) | Enter release-coordinator mode (see below) |
| **X** / Escape | Close the manager |

The single-key shortcuts (C / S / T / U) are configurable — see **Shortcuts** under Widget Settings. The **☰** options menu and **Release** button sit in the top-right corner and are click-only.

### Add Comment

Select a ticket and press **C** (or click Add Comment). Type your comment in the popup and press Enter. The comment is posted to Jira immediately.

### Change Status

Select a ticket and press **S** (or click Change Status). The tool fetches the available transitions for that issue (these depend on your Jira workflow) and shows them in a list. Select one and click **Apply** (or press Enter) to transition the issue.

### Create Subtask

Select a ticket and press **T** (or click Subtask). The same **title-first** new-ticket flow opens, with the selected ticket pre-set as the **parent**. Save it as a draft or continue to the full form.

### Options (☰) — sort & group by epic

Click the **☰** button in the top-right corner to:

- **Sort by** Priority, Due date, Status, Assignee, or Key (ascending or descending). The default is most-recently-updated.
- **Group by epic** — switch the flat list to a collapsible tree where your tickets are nested under their epic. The epic appears as a (non-actionable) grouping header even though it isn't assigned to you; only *your* tickets are listed under it. Toggle it off to return to the flat list. Handy when your tickets span several epics; leave it off for a quick flat view.

### Release mode

> **Release mode is disabled until you validate your statuses.** In Config → App Settings →
> Release settings, click **Test statuses** — it confirms your Pre-Release and Completed
> status names actually exist in the project. Only after that passes does the **Release**
> button become enabled. (Editing either status afterwards disables it again until you
> re-test.) If a status is left blank, Release mode shows a "not configured" error rather
> than an empty list.

Click **Release** for a coordinator view of everyone's tickets in the active sprint that are in a chosen status (default **"Ready for Release"**, configurable — see Widget Settings). From here you can:

- **Copy Keys** — copy all matching ticket keys to the clipboard as a **comma-separated** list.
- **Copy Users** — copy the unique assignee **emails** to the clipboard (falls back to display name where Jira doesn't expose the email).
- **Bulk → Done** — opens a **checklist** of the listed tickets (all checked); uncheck any that aren't actually complete, then move the rest to the configured done status (default **"Done"**) in one action.
- **Update** — refetch; **Back** / Escape returns to the manager.

---

## Planning Initiatives (graph)

Press **G** (Plan Initiative) to open the **initiative planner** — a visual canvas for
sketching out an epic and the stories/tasks/subtasks under it as a connected graph, *before*
anything goes to Jira. Everything stays as **local drafts** until you push.

You first see a list of saved plans; **New plan** names a fresh canvas, **Open** reopens one, and
**Open from Jira** loads an *existing* ticket straight from Jira — the issue, its children, and
the dependency links between them appear as a ready-made graph you can extend **and edit** (see
*Editing pushed content* below). The name you give a new plan also **seeds an Epic node** on the
canvas (titled with that name) so the initiative starts with an anchor instead of a blank canvas
— it's an ordinary draft you can edit, fill in, or delete.

Plans are stored as YAML under `<data folder>/plans/` only **while they hold local work** (draft
nodes or unpushed links). After a fully successful push the local plan file is **removed** — Jira
is the source of truth from then on, and **Open from Jira** rebuilds the graph on demand. A graph
that is purely hydrated from Jira is never written to disk at all.

Nodes still missing **required fields** are flagged on the canvas (a red border and a `⚠`
prefix); fill them in to clear the flag. A **Push to Jira** is blocked while any node is
incomplete, and tells you exactly which nodes are missing which fields — nothing is created
until the whole plan is valid.

**Connections are made on the canvas, not with buttons.** Hover any node and four translucent
arrows appear off its sides (with a little whitespace between the node and each arrow). Click an
arrow to add a **connected ticket** in that direction: a small dialog asks for the **title** and
the **relationship** — **Child** is the default; **Parent** and every dependency-link phrase your
Jira defines (*blocks / is blocked by / relates to /* …) are in the dropdown. From there,
**Create** continues the normal flow (pick a type, fill the form), or **Import** searches Jira
(the title you typed pre-fills the search) and drops the existing ticket in instead.

The toolbar is intentionally small:

| Action | What it does |
|---|---|
| **Add Ticket** | Independent node, no relationship: title dialog → **Create** (type + form) or **Import** an existing Jira ticket |
| **≡ grip (top-right corner)** | Click-hold and drag to **move** the node |
| **◢ grip (bottom-right corner)** | Click-hold and drag to **resize** the node |
| **Click a node or a line** | Select it — the node body only selects (it never drags); relationship arrows are selectable too (enables Edit / Delete) |
| **Edit** | Node selected: edit its fields. Arrow selected: change the relationship type |
| **Delete** | Node selected: remove it and its connections. Arrow selected: remove just that relationship |
| **Save** | Persist the plan to disk (also auto-saved on close while it holds local work) |
| **Push to Jira** | Apply everything — creations, edits, removals — to Jira (see below) |

Edges are drawn with **arrowheads** so direction is clear (hierarchy points parent → child; a
link points source → target). Hierarchy type is still inferred — an **Epic**'s child is
*epic-linked*, any other parent's child is a *subtask* — and a node has exactly one parent
(choosing Child/Parent for a node that already has one re-nests it).

### Editing pushed content (two-way)

Everything on the canvas is editable, including tickets that already exist in Jira:

- **Fields** — Edit an existing node and save; it gets an orange `✎` border and the *changed
  fields* are queued. Nothing touches Jira until you Push.
- **Relationships** — select a pushed arrow and Delete (or Edit to re-type it); the removal is
  queued the same way. Deleting a node never deletes the Jira ticket — only its relationships.

### Push to Jira

Creates the draft issues **parents-first** (so a child's epic/parent link resolves), writing each
new Jira key back onto its node; then applies queued **field updates**, **relationship
removals**, and new dependency links. It reports the created keys and everything else it did,
listing any failures.

> **Review before it happens:** if the push would touch tickets already in Jira, you first get a
> **visual diff** — the same graph rendered with **green** = will be created, **orange ✎** =
> fields will update, **green arrows** = new relationships, **red ✕** = relationships being
> removed — and a **"Push all changes?" Yes/No**. Below the graph, the changes are listed
> grouped per ticket: the key (or new title) on its own line, each change indented under it
> (`Summary: 'old' → 'new'`, `Create (Bug)`, `Relationship: …`). Pure-new plans keep the simple
> confirmation.

- **Everything succeeded** → the local plan file is **deleted**. Jira now holds the whole tree;
  use **Open from Jira** any time you want to see, extend, or edit it again.
- **Partial failure** → the plan is kept, with every node, link, edit, and removal that *did*
  land marked as done. Fix the problem and push again — only the failed remainder is attempted,
  so nothing is duplicated.

> **Disabling the planner.** Set the environment variable `JIRAMAXX_DISABLE_PLANNER` (to any
> value) to hide the **G** button and shortcut for a given deployment. Unset it to bring the
> feature back.

---

## Configuration Editor

Press **C** on the main window to open the full config editor. Changes take effect immediately on Save — no restart needed.

### Jira tab

Credentials and connection settings. See the Setup section above.

### Widget Settings tab

| Setting | Notes |
|---|---|
| **Data folder** | One folder holding drafts and the sprint snapshot cache (config.yaml also lives here). Supports `~` expansion. Default: `~/.jiramaxx`. Use the **Browse** button to pick a folder. Move or clear this one folder to relocate/reset all local state. |
| **UI Theme** | Any valid PySimpleGUI theme name, e.g. `DarkBlue3`, `LightGrey1`, `Reddit`. |
| **Hotkey: Create** | Global hotkey to open the main window. Default: `ctrl+alt+j` |
| **Hotkey: Manage** | Global hotkey to open the sprint manager directly. Default: `ctrl+alt+m` |
| **Shortcut: Comment / Status / Subtask / Update** | In-window single-key shortcuts in the sprint manager. Defaults: `c` / `s` / `t` / `u`. |

Under **Release settings** (own section at the bottom of the tab):

| Setting | Notes |
|---|---|
| **Pre-Release Status** | The status Release mode filters the sprint to. Default: `Ready for Release`. Set this to match your workflow. |
| **Completed / Post-Release Status** | The status Release mode's "Bulk → Done" transitions tickets to. Default: `Done`. |
| **Test statuses** | Confirms both statuses above exist in the project. Release mode stays disabled until this passes; editing a status disables it again until re-tested. |

Theme changes take effect the next time you open a window. Global hotkeys require a daemon restart; in-window shortcuts apply the next time you open that window.

### Ticket Types tab

Controls which fields appear in the form for each ticket type, and whether they are required or optional.

- **Required** — field appears in the form and must be filled before submitting
- **Optional** — field appears in the form but can be left blank
- **Not in Form** — field is hidden entirely for this ticket type

#### Reordering fields

Fields appear in the form in the order listed here. Use the **↑** and **↓** buttons to change the order within Required or Optional.

#### Moving fields between lists

- Select a field in **Not in Form** and click **→ Req** or **→ Opt** to add it to the form
- Select a field in **Required** or **Optional** and click **✕** to move it back to Not in Form

Changes apply to all new tickets. Open drafts retain the field layout they were created with.

---

## Hotkeys (daemon mode)

| Hotkey | Action |
|---|---|
| `ctrl+alt+j` | Open main window (new ticket / drafts) |
| `ctrl+alt+m` | Open sprint ticket manager |

Both hotkeys are configurable in Config → App Settings. Changes require a daemon restart to take effect (the GUI mode picks up hotkey changes immediately on next launch).

---

## config.yaml reference

```yaml
jira:
  base_url: https://yourcompany.atlassian.net
  api_token: '@keyring'     # token lives in the OS credential store; a literal token also works
  user_email: you@example.com
  project_key: ENG
  my_account_id: 61c8a3b2f1e4d500685e1234
  custom_fields:
    story_points: customfield_10016
    epic_link:    customfield_10014
    epic_name:    customfield_10011

paths:
  base_dir: ~/.jiramaxx     # one folder for drafts + sprint snapshots (and config.yaml)

ui:
  theme: DarkBlue3

hotkeys:                    # global daemon hotkeys (restart to apply)
  create_ticket:  ctrl+alt+j
  manage_tickets: ctrl+alt+m

shortcuts:                  # in-window single-key shortcuts (apply on next open)
  comment:      c
  status:       s
  subtask:      t
  update:       u

release:                    # release-coordinator mode (Manage → Release)
  filter_status: Ready for Release   # status it lists
  done_status:   Done                # status "Bulk → Done" transitions to
  validated:     false               # set by "Test statuses"; Release disabled until true

network:
  use_system_certs: true   # trust OS-installed (corporate) root CAs
  ca_bundle: ''            # optional path to a PEM bundle; overrides the above
  proxy: ''                # optional http://user:pass@host:port; blank = use env

ticket_types:
  Story:
    required: [summary, story_points, description]
    optional: [assignee, labels, sprint, epic_link, priority]
  Bug:
    required: [summary, description, severity, steps_to_reproduce]
    optional: [assignee, labels, priority]
  Task:
    required: [summary, description]
    optional: [assignee, story_points, labels, priority]
  Epic:
    required: [summary, description, epic_name]
    optional: [labels, priority]
  Initiative:
    required: [summary, description]
    optional: [labels, priority]
```

The `ticket_types` section is managed by the Config GUI. Hand-editing it is safe as long as field names match those in the Field reference table above.

---

## Corporate networks

Open **Config → Network** to configure these, or edit the `network` section of `config.yaml`.

**Certificates (TLS interception).** Many corporate networks inspect TLS through a proxy that presents its own root CA. Python's `requests` verifies against a bundled cert list and **ignores the Windows certificate store**, so requests to `*.atlassian.net` / `api.atlassian.com` can fail with `certificate verify failed` even when IT installed the root CA system-wide. jiramaxx defaults `use_system_certs: true`, which makes it trust the **OS certificate store** (via `truststore`) so those corporate CAs are honored automatically. This is safe for home users — it just uses the standard public roots already in the OS store. If anything goes wrong, it falls back to default behavior.

- Need an explicit cert instead? Set **CA Bundle (PEM path)** (`network.ca_bundle`) or the `REQUESTS_CA_BUNDLE` environment variable.
- To opt out of OS-store trust, untick **Use the operating-system certificate store** (`use_system_certs: false`).

**Proxies.** If your network requires an outbound proxy, set **Proxy URL** (`network.proxy`), e.g. `http://proxy.corp:8080`. This simply exports `HTTP_PROXY` / `HTTPS_PROXY` (every case variant) for the app, so you don't need to embed credentials in the URL. Leave it blank to use the `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` environment variables you've already set in your shell, which `requests` honors automatically.

**Other things that can bite on a locked-down network:**
- **Egress allowlists** must permit `*.atlassian.net` and `api.atlassian.com`.
- **Authenticated proxies (NTLM/Kerberos)** and **PAC auto-config** are not handled directly — set `HTTP_PROXY` / `HTTPS_PROXY` yourself (with inline credentials if your proxy needs them), or run a local proxy bridge.
- **Recording** transcribes locally on the CPU — audio/text never leave the machine — but it downloads its Whisper model (~145 MB) from Hugging Face on first use, then runs offline. On a locked-down network, allow that one-time download, or pre-fetch the model for an air-gapped install (see the `jiramaxx-recording` README).

---

## Troubleshooting

**400 Bad Request when submitting**
- Click **Show Stack Trace** on the error dialog to see the full Jira response and the exact payload sent.
- Most common causes: wrong project key (use Browse to verify), incorrect custom field IDs (check Project Settings → Fields in Jira), `epic_name` included on a non-Epic ticket type.

**Story points not saving to Jira**
- Check the Story Points custom field ID in Config → Jira. The default (`customfield_10016`) is not universal.

**Assignee field not working**
- The Assignee field requires the user's **Atlassian account ID** (a 24-character hex string), not their name or email. Jira Cloud's v3 API does not accept `name`-based assignees.

**Hotkeys not triggering**
- On Windows, the `keyboard` library may require running the terminal as Administrator.
- Confirm the hotkey string format: modifiers and keys are separated by `+`, e.g. `ctrl+alt+j`.

**Manage shows no tickets (or fewer than expected)**
- The manager lists only issues in the **active sprint** that are **assigned to you**. Tickets in future sprints or assigned to others won't appear here (use **Release** mode to see all assignees in a given status).
- It opens from a cached snapshot — press **U** (**Update**) to refetch from Jira.
- Confirm your project has an **active** sprint (a started sprint, not just future ones).

**Disabling the recording feature in a managed environment**

Recording is a separate, optional package (`jiramaxx-recording`). The strongest control is simply not to mirror it — core `jiramaxx` then has no recording code at all. As an additional belt-and-braces switch (e.g. if a user installs the package themselves), set the environment variable `JIRAMAXX_DISABLE_RECORDING=1` (accepts `1`, `true`, `yes`, or `on`). When set, the Record button is disabled and the Recording tab shows a "disabled by environment policy" message instead of the controls. Suitable for group policy / device baseline configuration.

**Recording fails with `AssertionError` from `soundcard/mediafoundation.py` (e.g. `wFormatTag == 0xFFFE`)**

The `soundcard` library expects the device to report its default audio format as `WAVE_FORMAT_EXTENSIBLE`. Some headsets (notably wireless ones like the SteelSeries Arctis line) report a different format tag and trip a hardcoded assertion. To fix:

1. Open Windows Sound settings (Win+R → `mmsys.cpl`)
2. **Recording** tab → find the affected device → **Properties**
3. **Advanced** tab → **Default Format** dropdown
4. Pick something explicit like `2 channel, 16 bit, 48000 Hz (DVD Quality)` — anything in this dropdown will normally be reported as EXTENSIBLE
5. Apply, OK
6. Re-test recording in jiramaxx

If swapping to your laptop's built-in microphone works without this change, the problem is specific to that device's driver and the format override above is the durable fix.
