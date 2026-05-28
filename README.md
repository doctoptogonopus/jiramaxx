# Jiramaxx

A lightweight desktop GUI for creating and managing Jira tickets without leaving your keyboard. Runs as a background daemon and pops up on a global hotkey.

---

## Requirements

- Python 3.10+
- A Jira Cloud account with an API token

```
poetry install
```

---

## Setup

### 1. First run

```
jiramaxx
OR
jiramaxx --gui
```

On first run with no `config.yaml`, a default one is created and the widget requires you to fill in your credentials (see below) and run again, or use the in-widget Config screen.
_If running with --gui flag, the script will stop running upon exiting rendering shortcuts unavailable._

### 2. Configure credentials

Open the Config screen (press **C** on the main window or run `python main.py --gui` and press C), go to the **Jira** tab.

| Field | What to enter |
|---|---|
| **Base URL** | Your Atlassian domain, e.g. `https://yourcompany.atlassian.net` |
| **API Token** | Generated at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| **User Email** | The email address on your Atlassian account |
| **Project Key** | The short key for your project (e.g. `ENG`, `JCSD`). Use the **Browse** button to find it. |
| **Board ID** | The numeric ID of your Scrum/Kanban board. Found in the URL when viewing your board: `.../jira/software/projects/XXX/boards/2` — the trailing number is the board ID. |
| **My Account ID** | Your Atlassian account ID. Found at: Jira → Profile → the ID in the URL, or via `GET /rest/api/3/myself`. Used when assigning tickets to yourself. |

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
| **C** | Open the configuration editor |
| **Q** / Escape | Quit |

The draft counter updates automatically after every action.

---

## Creating a Ticket

1. Press **N** (or click New Ticket).
2. Select a ticket type — keyboard shortcuts **1–5** or the **first letter** of the type name (S, B, T, E, I) also work.
3. Fill in the form. Required fields are marked with `*`. Use **Tab / Shift+Tab** to move between fields (Tab does not insert whitespace in multiline fields).
4. Choose an action:

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

Drafts are stored as YAML files in `~/.jiramaxx/cache/` (configurable). A draft is created any time you click **Save Draft**. After a successful Jira submission the ticket is also retained locally (marked submitted) so you have a local record.

### Draft list actions

| Button | Action |
|---|---|
| **Open** / Enter / double-click | Re-open the ticket form to continue editing or submit |
| **Delete** | Permanently removes the local draft (prompts for confirmation) |
| **Cancel** / Escape | Returns to the main window |

---

## Managing Existing Tickets

Press **M** to open the sprint ticket manager. It loads all issues from the active sprint on your configured board.

The first ticket is selected automatically — use the **↑ / ↓ arrow keys** to navigate the list without clicking.

| Button / Key | Action |
|---|---|
| **C** | Add a comment to the selected ticket |
| **S** | Change the status of the selected ticket |
| **X** / Escape | Close the manager |

### Add Comment

Select a ticket and press **C** (or click Add Comment). Type your comment in the popup and press Enter. The comment is posted to Jira immediately.

### Change Status

Select a ticket and press **S** (or click Change Status). The tool fetches the available transitions for that issue (these depend on your Jira workflow) and shows them in a list. Select one and click **Apply** (or press Enter) to transition the issue.

---

## Configuration Editor

Press **C** on the main window to open the full config editor. Changes take effect immediately on Save — no restart needed.

### Jira tab

Credentials and connection settings. See the Setup section above.

### Widget Settings tab

| Setting | Notes |
|---|---|
| **Cache Directory** | Where local drafts are stored. Supports `~` expansion. Default: `~/.jiramaxx/cache` |
| **UI Theme** | Any valid PySimpleGUI theme name, e.g. `DarkBlue3`, `LightGrey1`, `Reddit`. |
| **Hotkey: Create** | Global hotkey to open the main window. Default: `ctrl+alt+j` |
| **Hotkey: Manage** | Global hotkey to open the sprint manager directly. Default: `ctrl+alt+m` |

Theme changes take effect the next time you open a window.

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
  api_token: YOUR_API_TOKEN
  user_email: you@example.com
  project_key: ENG
  board_id: '2'
  my_account_id: 61c8a3b2f1e4d500685e1234
  custom_fields:
    story_points: customfield_10016
    epic_link:    customfield_10014
    epic_name:    customfield_10011

cache:
  directory: ~/.jiramaxx/cache

ui:
  theme: DarkBlue3

hotkeys:
  create_ticket:  ctrl+alt+j
  manage_tickets: ctrl+alt+m

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

**"No active sprint tickets found"**
- Verify your Board ID is correct. It appears in the Jira board URL: `.../boards/2` → `2`.
- Confirm the board has an active sprint (not just future sprints).

