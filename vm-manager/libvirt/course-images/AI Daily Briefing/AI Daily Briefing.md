# AI Daily Briefing v2 for AiVirt

# Agent Runbook Scope

<aside>
🤖

Agent-only operational reference. The canonical student tutorial is [AI Daily Briefing v2](https://app.notion.com/p/AI-Daily-Briefing-v2-3c02f32c80dd8082b337d172405d6744?pvs=21). Do not edit the student tutorial from this page. Preserve student work and secrets.

</aside>

**Fixed identity:** Ubuntu 22.04 · n8n 2.31.7 · `~/aivirteach/ai-daily-briefing` · container `ai-daily-briefing-n8n` · volume `ai_daily_briefing_n8n_data` · `http://localhost:5678` · timezone `Asia/Kuala_Lumpur`.

**Sync note:** operational commands and node settings are synchronized with the canonical tutorial as of 2026-08-19. Page-specific attachment references intentionally remain local to this agent page.

---

# Agent Operating Protocol

<aside>
🤖

This page is the agent-facing runbook. Preserve the user's existing work, identify the highest verified checkpoint, and continue from that checkpoint instead of restarting the tutorial.

</aside>

## Agent Interaction Rules

1. **Determine state before instructing.** Ask for the smallest useful terminal output or n8n node output that proves the current checkpoint.
2. **Use contiguous progress.** Mark a checkpoint complete only when its expected result is visible or logically required by a later verified result.
3. **Separate configuration from execution.** A configured node is not complete until it has executed successfully. A scheduled workflow is not complete until it is published.
4. **Protect secrets.** Never ask the user to paste Tavily keys, Gemini keys, Gmail Client Secrets, access tokens, refresh tokens, or full credential screenshots. Ask for redacted screenshots or error text only.
5. **Diagnose locally.** Re-run the nearest failed command or node. Do not restart the entire tutorial unless earlier state is missing or corrupted.
6. **Change one variable at a time.** After a fix, repeat the checkpoint that failed and compare the new output.
7. **Do not treat zero items as success.** A green node with zero output can still block every downstream node.
8. **Protect legacy deployments.** The fixed Daily Briefing identity is directory `~/aivirteach/ai-daily-briefing`, container `ai-daily-briefing-n8n`, volume `ai_daily_briefing_n8n_data`, and host port `5678`. If another container or project already owns port 5678, treat it as existing user work and inspect it before stopping, renaming, or migrating anything.

## Progress States

| State | Verified milestone |
| --- | --- |
| `S0` | No usable evidence yet |
| `S1` | Docker Engine and Compose are available |
| `S2` | n8n container is running and the editor opens |
| `S3` | Manual Trigger executes successfully |
| `S4` | Tavily returns search results |
| `S5` | Articles are normalized, filtered, deduplicated, sorted, limited, and aggregated |
| `S6` | Gemini returns structured JSON |
| `S7` | Code node produces the email subject and HTML |
| `S8` | Gmail sends and the recipient receives the test email |
| `S9` | Schedule Trigger is configured and the workflow is published |

## Required Agent Reply Format

```
Current checkpoint: S0-S9
Evidence: what proves this checkpoint
Next action: one concrete action
Expected result: exact output or UI state
If it fails: the specific evidence the user should return
```

---

# 1 Configure the Runtime Environment

# 1.1 Install Docker

Install Docker Engine and Compose from Docker’s official Ubuntu repository:

```bash
sudo apt update
sudo apt install -y ca-certificates curl

sudo install -m 0755 -d /etc/apt/keyrings

sudo curl -fsSL \
  https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update

sudo apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

sudo systemctl enable --now docker
```

# 1.2 Install and Start n8n

**Fixed settings:** n8n `2.31.7` · `http://localhost:5678` · container `ai-daily-briefing-n8n` · volume `ai_daily_briefing_n8n_data` · directory `~/aivirteach/ai-daily-briefing`.

```bash
mkdir -p ~/aivirteach/ai-daily-briefing

cd ~/aivirteach/ai-daily-briefing

sudo docker volume create ai_daily_briefing_n8n_data

nano compose.yaml

sudo docker compose config

sudo docker compose up -d

sudo docker compose ps

curl --noproxy "*" -fsS \
  http://127.0.0.1:5678/healthz/readiness >/dev/null \
  && echo "n8n is ready"
```

Paste this into `compose.yaml`:

```yaml
services:
  n8n:
    image: docker.n8n.io/n8nio/n8n:2.31.7

    container_name: ai-daily-briefing-n8n

    restart: unless-stopped

    ports:
      - "127.0.0.1:5678:5678"

    environment:
      TZ: Asia/Kuala_Lumpur

      GENERIC_TIMEZONE: Asia/Kuala_Lumpur

      N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS: "true"

      N8N_RUNNERS_ENABLED: "true"

    volumes:
      - ai_daily_briefing_n8n_data:/home/node/.n8n

volumes:
  ai_daily_briefing_n8n_data:
    external: true
```

Save and exit Nano: press `Ctrl + O`, press `Enter`, and then press `Ctrl + X`.

Open [http://localhost:5678](http://localhost:5678), create the owner account, and **Create Workflow**.

# 2 Build the AI Daily Briefing Workflow

# 2.1 Trigger

Add a Manual Trigger node, click Execute Workflow, confirm the node succeeds, and save. Keep this trigger for later testing.

# 2.2 Retrieve News from the Internet

Create an account at [Tavily](https://app.tavily.com), copy the private `tvly-...` key, then add a Tavily **node→ Search** node and save the key as a Tavily API credential.

```
Resource: Search

Operation: Query
Query: latest important developments in artificial intelligence, AI models, semiconductors, cybersecurity, and technology companies

Topic: News
Search Depth: Basic
Days: 1
Max Results: 10
```

Execute the node.

# 2.3 Organize the News Data

Add Split Out node: `Fields To Split Out = results`; `Include = No Other Fields`. Execute the node.

Add Edit Fields node in Manual Mapping mode, turn off Include Other Input Fields, and create:

```
title       | String | {{ $json.title }}
link        | String | {{ $json.url }}
summary     | String | {{ $json.content }}
source      | String | {{ $json.url.extractDomain() }}
publishedAt | String | {{ $json.published_date || $json.publishedDate || '' }}
score       | Number | {{ $json.score }}
```

Execute the node.

# 2.4 Clean, Deduplicate, Sort, and Limit Candidate Articles

Add and execute these nodes in order:

Add Filter node: 

```
{{ $json.title }}   | String | is not empty (AND)
{{ $json.link }}    | String | is not empty (AND)
{{ $json.summary }} | String | is not empty
```

Add Remove Duplicates node: `Operation = Remove Items Repeated Within Current Input`

Add Remove Duplicates node: `Operation = Remove Item Processed in Previous Executions` 

Add Sort node: `Type = Simple`; `Field Name = score`; `Order = Descending` 

Add Limit node: `Max Items = 5`; `Keep = First Items`

Add Aggregate node:`Aggregate = All Item Data (Into a Single List)`; `Put Output in Field = articles`; `Include = Specified Fields`; `Fields = title, link, summary, source, publishedAt, score`

# 2.5 Use Gemini to Generate Structured News Summaries

Add a HTTP Request node:

```
Method: POST
URL: https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent
Authentication: Generic Credential Type → Header Auth
Header: x-goog-api-key = your Google AI Studio API key (Create the API key in [Google AI Studio](https://aistudio.google.com/api-keys))
Allowed HTTP Request Domains: All
Send Body: On
Body Content Type: JSON
Specify Body: Using JSON
```

JSON: 

```jsx
{{ JSON.stringify({

  contents: [
    {
      parts: [
        {
          text: `You are an English technology news editor creating a daily AI briefing.

Analyze every candidate article supplied below.

Candidate articles:

${JSON.stringify($json.articles, null, 2)}

For each article:

1. Assign an importanceScore from 1 to 10.
2. Rewrite the headline in clear English using no more than 20 words.
3. Write an English summary containing 40 to 80 words.
4. Assign exactly one category from:
   AI
   Chips
   Tech Business
   Cybersecurity
   Other
5. Write one concise English sentence explaining why the article is important.
6. Preserve the original source, publishedAt, and link exactly.
7. Use only information contained in the supplied article.
8. Do not invent facts, statistics, quotations, organizations, products, or conclusions.
9. Return one result for every supplied article.

Return exactly this JSON structure:

{
  "results": [
    {
      "importanceScore": 1,
      "headline": "",
      "summary": "",
      "category": "",
      "reason": "",
      "source": "",
      "publishedAt": "",
      "link": ""
    }
  ]
}

Return JSON only.
Do not use Markdown.
Do not wrap the response in a code block.`
        }
      ]
    }
  ],

  generationConfig: {

    responseMimeType: "application/json",

    maxOutputTokens: 4000
  }

}) }}
```

Execute the step.

# 2.6 Parse Gemini Output and Generate the AI Daily Briefing

Add a Code node: `Mode = Run Once for All Items`; `Language = JavaScript`. 

Download the source file below, open it, and paste the complete contents into the Code node:

[AI_Daily_Briefing_Code.txt](AI_Daily_Briefing_Gemini_Code_No_Comments.txt)

Execute the node.

# 2.7 Send the Email and Enable Daily Automatic Execution

Add a Gmail node and create an OAuth credential

1. In [Google Cloud Console](https://console.cloud.google.com/) → My first project = `named by n8n Gmail Automation` → create → APIs & Services → library → search and enable Gmail API
2. google auth platform → get started → app name = `n8n Gmail Automation` → User support email = xxx@gmail.com → next → external → next → Developer support email = xxx@gmail.com → next → create
3. test users → add users = xxx@gmail.com
4. data access → Add or remove scopes → add “`https://www.googleapis.com/auth/gmail.send`” → update → save
5. clients → create → Application type = Web application → Name = `n8n Gmail Client` → Authorized redirect URIs = `http://localhost:5678/rest/oauth2-credential/callback`→ create
6. audience → publish app → confirm 
7. Copy the Client ID and Client Secret from clients into n8n, save and sign in with Google

Configure the Gmail node:

```
Resource: Message
Operation: Send
To: recipient email address
Subject: {{ $json.subject }}
Email Type: HTML
Message: {{ $json.html }}
```

Execute once and verify the email. 

Then add Schedule Trigger node in front of Tavily node: 

```
Trigger Interval: Days
Days Between Triggers: 1
Trigger at Hour: 8
Trigger at Minute: 0
```

Keep the Manual Trigger node for future manual testing.

After confirming that the workflow runs successfully, click Publish to enable scheduled execution.

---

# Agent Verification and Recovery Guide

Use this section to identify the user's current progress, verify the expected result, and select the smallest recovery action.

## S1 — Docker Ready

**Expected result**

- `docker --version` prints a Docker version.
- `sudo docker compose version` prints a Docker Compose version.
- `sudo systemctl is-active docker` returns `active`.

**Evidence to request**

```bash
docker --version
sudo docker compose version
sudo systemctl is-active docker
```

**Common failures**

- `Unable to locate package docker-ce`: the Docker repository was not loaded. Check `/etc/apt/sources.list.d/docker.sources`, then run `sudo apt update`.
- `NO_PUBKEY`, `repository is not signed`, or a missing `docker.asc`: repeat the key download and permission steps.
- `Could not resolve`, `Temporary failure in name resolution`, or curl exit code 6: network or DNS is blocking the Docker repository.
- `Cannot connect to the Docker daemon`: run `sudo systemctl enable --now docker`, then check `sudo systemctl status docker --no-pager`.
- `permission denied` for the Docker socket: use `sudo docker ...` in this tutorial.

**State decision**

- All three checks pass → mark `S1`.
- Docker exists but the service is not active → remain at `S0` and repair the service.

## S2 — n8n Running

**Expected result**

- `sudo docker compose config` finishes without a YAML error.
- `sudo docker compose ps` shows service `n8n` with container `ai-daily-briefing-n8n` as `Up` or `running`.
- The readiness request exits successfully and prints `n8n is ready`.
- `http://localhost:5678` opens the n8n owner-account or workflow page inside the virtual machine.

**Evidence to request**

```bash
cd ~/aivirteach/ai-daily-briefing
sudo docker compose ps
sudo docker compose logs --tail=50 n8n
sudo docker volume inspect ai_daily_briefing_n8n_data
sudo docker ps --format "table {{.Names}}\t{{.Ports}}\t{{.Status}}"
```

Ask whether `http://localhost:5678` opens. Request a screenshot only if the browser message is important.

**Common failures**

- `yaml: line ...` or `services must be a mapping`: indentation or punctuation in `compose.yaml` is invalid. Ask for the file with secrets removed.
- `external volume "ai_daily_briefing_n8n_data" not found`: run `sudo docker volume create ai_daily_briefing_n8n_data`.
- `port is already allocated`: another process or container is using host port 5678. Ask for `sudo docker ps --format "table {{.Names}}\t{{.Ports}}\t{{.Status}}"`. If an older Daily Briefing container is present, preserve it and its volume; do not stop, remove, or migrate it until its ownership and data are confirmed.
- Container is `Restarting` or `Exited`: use the final 50 log lines to diagnose before changing the Compose file.
- Container is running but the page does not open: confirm the browser is inside the same virtual machine and use `http://127.0.0.1:5678`.

**State decision**

- Container is running and the editor opens → mark `S2`.
- Compose validates but the container is not running → remain at `S1`.

## S3 — Manual Trigger Verified

**Expected result**

- The workflow contains a Manual Trigger.
- Clicking **Execute Workflow** produces a green success indicator.
- The workflow is saved.

**Evidence to request**

- A screenshot showing the Manual Trigger and its successful execution, or a clear statement that the node is green and the workflow was saved.

**Common failures**

- The editor does not execute: confirm the n8n page is still connected and refresh it once.
- The user created a different trigger: add Manual Trigger and keep it even after Schedule Trigger is added.

**State decision**

- Manual Trigger executes successfully → mark `S3`.

## S4 — Tavily Results Available

**Expected result**

- The Tavily node executes successfully.
- Its output contains a `results` array with 1–10 items.
- Articles normally contain `title`, `url`, `content`, and `score`.

**Evidence to request**

- Item count and a redacted sample of one result.
- For an error, request the HTTP status and message, never the API key.

**Common failures**

- `401` or `Unauthorized`: the credential is missing, invalid, or attached to the wrong node.
- `403`: the account or credential is not permitted to use the requested operation.
- `429`: Tavily quota or rate limit was reached; wait or inspect account usage.
- Zero results with a successful node: broaden the query or increase the time window before changing downstream nodes.
- Tavily node is unavailable: confirm the fixed n8n version and search the node picker again.

**State decision**

- A non-empty `results` array exists → mark `S4`.
- The node is green but `results` is empty → remain at `S3`.

## S5 — Article Pipeline and Aggregate Verified

**Expected result by node**

1. **Split Out:** one n8n item per Tavily result.
2. **Edit Fields:** each item contains `title`, `link`, `summary`, `source`, `publishedAt`, and numeric `score`.
3. **Filter:** every remaining item has non-empty `title`, `link`, and `summary`.
4. **Remove Duplicates:** duplicate links are removed.
5. **Sort:** scores are ordered from highest to lowest.
6. **Limit:** output contains no more than five items.
7. **Aggregate:** exactly one item is produced with an `articles` array containing 1–5 article objects.

**Evidence to request**

- The item count after the last successful node.
- For Aggregate, a redacted output showing `articles: [...]` and one sample object.

**Common failures**

- Split Out cannot find `results`: the Tavily output shape is different or the wrong node is connected.
- Edit Fields shows `undefined`: inspect the incoming Tavily field names and correct only the affected expression.
- All items disappear at Filter: inspect one pre-filter item for empty `title`, `link`, or `summary`.
- The second Remove Duplicates returns zero items on a repeated test: previously processed links may be stored in node history. This is not necessarily a broken workflow. During testing, clear the node's stored deduplication history, temporarily bypass the second deduplication node, or use a query that returns new links.
- Aggregate output uses another field name: set **Put Output in Field** to `articles`.
- Aggregate returns zero items: locate the first upstream node whose output count became zero.

**State decision**

- One Aggregate item contains a non-empty `articles` array → mark `S5`.
- Otherwise, remain at the last verified node inside this stage and resume there.

## S6 — Gemini Structured Output Verified

**Expected result**

- The HTTP Request node returns status `200`.
- `candidates[0].content.parts[0].text` contains JSON text.
- The parsed JSON has a non-empty `results` array with `importanceScore`, `headline`, `summary`, `category`, `reason`, `source`, `publishedAt`, and `link`.

**Evidence to request**

- HTTP status, `finishReason`, and a redacted response excerpt.
- Never request the `x-goog-api-key`.

**Common failures**

- `400`: malformed request JSON, an unsupported parameter, or an invalid body expression.
- `401` or `403`: missing/invalid API key, API access restriction, or project permission problem.
- `404`: verify the model ID is exactly `gemini-3.5-flash-lite`.
- `429`: quota or rate limit; wait or inspect Google AI Studio usage.
- Gemini receives no articles: return to Aggregate and verify `$json.articles`.
- `finishReason` is not `STOP`: treat the response as incomplete and retry only after checking the reason.

**State decision**

- HTTP 200 plus a non-empty structured `results` array → mark `S6`.

## S7 — Email HTML Generated

**Expected result**

- The Code node outputs exactly one item.
- Output contains non-empty `subject` and `html`.
- `articleCount` is normally 1–5 and matches the number of normalized `results`.
- The subject follows `AI Daily Briefing - YYYY-MM-DD`.

**Evidence to request**

- The Code node output keys and `articleCount`.
- Request only a short HTML preview, not the entire message, unless HTML generation itself is being debugged.

**Common failures**

- `No input was received`: the Gemini node did not execute or is not connected.
- `Gemini response was incomplete`: inspect `finishReason` in S6.
- `Gemini returned no usable text`: the API response path is missing or changed.
- `Gemini output is not valid JSON`: inspect the raw Gemini text for extra text, truncation, or malformed JSON.
- `results array` missing or empty: return to the Gemini prompt and response.
- `All Gemini results were removed`: every result lacks a usable headline or summary.

**State decision**

- One item contains valid `subject`, `html`, and `articleCount` → mark `S7`.

## S8 — Gmail Delivery Verified

**Expected result**

- Gmail credential connects successfully.
- The Gmail node executes with a green success indicator and returns a message identifier.
- The recipient receives an HTML email with the expected subject and article cards.

**Evidence to request**

- The Gmail node status and redacted output.
- Confirmation that the email arrived; if not, ask the user to check Spam and the recipient address.
- Never request Client Secret, access token, or refresh token.

**Common failures**

- `redirect_uri_mismatch`: the Google OAuth client's Authorized redirect URI does not exactly match `http://localhost:5678/rest/oauth2-credential/callback`.
- `access_denied`, unverified-app warning, or blocked access: confirm the correct Google account, Audience status, and app publishing state.
- `invalid_client`: Client ID or Client Secret was copied incorrectly.
- `invalid_grant`: reconnect the Gmail credential; the authorization may have expired or been revoked.
- `403 insufficient permissions`: verify Gmail API is enabled and the `gmail.send` scope was added.
- Node succeeds but no email arrives: verify recipient, Spam, and Gmail Sent items before changing OAuth.

**State decision**

- Node succeeds and the recipient confirms delivery → mark `S8`.
- Node succeeds but delivery is unconfirmed → do not mark `S8` yet.

## S9 — Scheduled Automation Published

**Expected result**

- Manual Trigger remains connected to Tavily for testing.
- Schedule Trigger is also connected to Tavily.
- Schedule parameters are Days / 1 / 8 / 0.
- The workflow is saved and shows a published or active production state.
- The workflow time zone is `Asia/Kuala_Lumpur`.

**Evidence to request**

- A screenshot showing both triggers, their connections, the schedule parameters, and the published state.
- After the first scheduled time, request the production execution record and email delivery confirmation.

**Common failures**

- Manual execution works but no scheduled execution occurs: the workflow is not published, the Schedule Trigger is disconnected, or production executions are disabled.
- Execution occurs at the wrong time: verify `GENERIC_TIMEZONE=Asia/Kuala_Lumpur` and the Schedule Trigger hour.
- Scheduled execution starts but stops after deduplication: the second Remove Duplicates may correctly reject previously processed links; inspect item counts.
- Scheduled execution reaches Gmail but no email is sent: return to S8 and inspect the Gmail production execution.

**State decision**

- Configuration and published state are visible → mark `S9 configured`.
- A production execution completes and delivers an email → mark `S9 verified`.

# Granular Expected Results and Detection Plan

<aside>
📡

Design only. No telemetry has been deployed, no tracking node has been added to the student's workflow, and the canonical tutorial has not been changed.

</aside>

## Checkpoint Catalog

| ID | Student action | Expected output / pass condition | Proposed detector | Tutorial impact |
| --- | --- | --- | --- | --- |
| P01 | Docker installed and service active | `docker --version` and `sudo docker compose version` print versions; `systemctl is-active docker` is `active`. | VM supervisor runs read-only shell probes. | No |
| P02 | Project directory created | `~/aivirteach/ai-daily-briefing` exists and is a directory. | VM supervisor checks the fixed path. | No |
| P03 | Persistent volume created | `docker volume inspect ai_daily_briefing_n8n_data` succeeds and returns that exact name. | Docker Engine read-only inspect. | No |
| P04 | Compose file valid | `docker compose config` exits 0 and resolves image `2.31.7`, container name, port, timezone, and external volume correctly. | VM supervisor parses normalized Compose output; never collect secrets. | No |
| P05 | n8n container running | Compose reports service `n8n` / container `ai-daily-briefing-n8n` as running or Up, with host port `127.0.0.1:5678`. | Docker Engine read-only inspect. | No |
| P06 | n8n ready | Readiness request exits 0 and prints `n8n is ready`. | VM supervisor polls the local readiness URL. | No |
| P07 | Editor reachable and owner setup complete | The editor or owner-setup page opens. Completion means an authenticated editor session can create a workflow. | HTTP probe proves reachability; authenticated state needs a platform/browser or n8n account signal. | Decision needed only for owner-setup telemetry |
| P08 | Manual Trigger added and executed | Workflow contains Manual Trigger; latest manual execution is successful and the node is green. | Read-only n8n workflow definition plus execution record. | n8n observer access required; tutorial text unchanged |
| P09 | Tavily configured and executed | Latest Tavily output has a non-empty `results` array with 1–10 items; a normal item has `title`, `url`, `content`, and numeric `score`. | n8n execution data: node status, item count, redacted field-shape validation. | n8n observer access required |
| P10 | Split Out executed | Output item count equals the Tavily `results` count; each output item represents one article. | n8n execution data and item counts. | n8n observer access required |
| P11 | Edit Fields executed | Every output item exposes exactly the required agent-visible fields: `title`, `link`, `summary`, `source`, `publishedAt`, `score`; score is numeric. | Schema-only inspection of redacted node output. | n8n observer access required |
| P12 | Filter executed | All remaining items have non-empty `title`, `link`, and `summary`; zero items is not success. | n8n execution data with validation predicates and item count. | n8n observer access required |
| P13 | Within-run deduplication executed | No duplicate `link` values remain in the current run. | Hash links inside the observer; store hashes only, not article content. | n8n observer access required |
| P14 | Cross-run deduplication executed | Node succeeds; output may be zero only when every link was processed previously. Record input and output counts. | n8n execution status and counts; correlate with earlier executions. | n8n observer access required |
| P15 | Sort executed | Scores are monotonically descending across all output items. | n8n execution data; compare adjacent numeric scores. | n8n observer access required |
| P16 | Limit executed | Output count is between 1 and 5 when upstream data is non-empty. | n8n execution item count. | n8n observer access required |
| P17 | Aggregate executed | Exactly one output item contains a non-empty `articles` array of 1–5 objects with the six required fields. | Schema-only inspection of redacted output. | n8n observer access required |
| P18 | Gemini executed | HTTP status is 200; `finishReason` is `STOP`; response text parses as JSON with a non-empty `results` array and all eight required fields. | n8n execution metadata and schema validation; never collect API keys or full content. | n8n observer access required |
| P19 | Code node executed | Exactly one output item contains non-empty `subject` and `html`; `articleCount` is 1–5 and matches normalized Gemini results. | n8n execution output key/type checks; do not transmit full HTML. | n8n observer access required |
| P20 | Gmail credential connected | Gmail node has a credential reference and can authenticate; no secret value is exposed. | Workflow definition can prove a credential reference; a successful send proves authentication. | n8n observer access required |
| P21 | Test email sent and received | Gmail node succeeds and returns a message ID; recipient confirms the expected HTML email arrived. | Execution record proves send; receipt needs inbox-side callback or explicit confirmation. | Decision needed for receipt telemetry |
| P22 | Schedule configured | Manual Trigger remains; Schedule Trigger is connected to Tavily with Days / 1 / 8 / 0 and timezone `Asia/Kuala_Lumpur`. | Read-only workflow topology and parameter inspection. | n8n observer access required |
| P23 | Workflow published | Workflow is saved and in published/active production state. | Read-only workflow metadata. | n8n observer access required |
| P24 | Scheduled production run verified | A production execution starts from Schedule Trigger, reaches Gmail successfully, and receipt is confirmed. | Execution observer plus inbox callback or confirmation. | Decision needed for receipt telemetry |

## Recommended Detection Architecture

1. **VM supervisor — P01–P07.** A lab-owned process outside the student's shell polls Docker, fixed paths, container state, and the local readiness endpoint every 5 seconds. It performs read-only checks and reports only normalized results.
2. **n8n observer — P08–P24.** A lab-owned, read-only integration inspects the saved workflow definition and recent execution records. It validates node type, connection, parameters, run status, item count, and output schema. It must redact credentials, request bodies containing keys, article bodies, and email HTML.
3. **Progress service.** The observer emits idempotent checkpoint events. The service calculates the highest contiguous checkpoint and separately records health regressions, so a temporarily stopped container does not erase historical completion.
4. **Receipt confirmation.** P21 and P24 cannot be proven solely by a successful Gmail API call. Use either a controlled test inbox callback or an explicit user confirmation.
5. **Do not add progress nodes to the student's n8n canvas by default.** They alter the target workflow, can be renamed or deleted, and may leak course metadata. Prefer out-of-band observation. Add in-workflow hooks only if the platform cannot read execution records.

Useful n8n references: [execution history](https://docs.n8n.io/workflows/executions/all-executions/), [API authentication](https://docs.n8n.io/api/authentication/), and [monitoring](https://docs.n8n.io/hosting/logging-monitoring/monitoring/).

## Event Contract

```json
{
  "courseId": "ai-daily-briefing-v2",
  "studentId": "opaque-platform-id",
  "vmId": "opaque-vm-id",
  "checkpointId": "P01-P24",
  "state": "passed | failed | unknown",
  "observedAt": "ISO-8601 timestamp",
  "evidenceType": "shell | docker | http | n8n-workflow | n8n-execution | receipt",
  "evidenceSummary": "redacted normalized fact",
  "workflowId": "optional",
  "executionId": "optional",
  "nodeName": "optional",
  "itemCount": 0,
  "errorCode": "optional",
  "attempt": 1
}
```

## Privacy and Integrity Rules

- Never collect Tavily keys, Gemini keys, Gmail Client Secrets, OAuth tokens, raw credential objects, full screenshots, full article bodies, or full email HTML.
- Prefer booleans, counts, type checks, hashes, node IDs, execution IDs, and short redacted errors.
- A green node with zero items is not a pass where the checkpoint requires non-empty output.
- Emit `passed` only when the expected result is observed. Emit `failed` for a concrete failed attempt, and `unknown` when evidence is unavailable.
- Advance the displayed course position only through contiguous passed checkpoints; keep later isolated passes as evidence but do not skip an unverified prerequisite.

## Decisions Required Before Implementation

- How the lab platform will provision a read-only n8n observer identity or API key without asking the student to handle it.
- Whether owner-account completion may be detected through the platform/browser session or should remain user-confirmed.
- Whether the course controls a test inbox for automatic receipt confirmation.
- Whether successful execution data is retained long enough on the self-hosted instance for node-level schema checks.
- Polling interval, event endpoint, retention period, and student consent language.
- If any of these choices require new setup commands, environment variables, a Webhook/Code node, or changes to execution retention, obtain approval before changing the student tutorial.

---

## Symptom-to-Checkpoint Matrix

| Symptom | Start diagnosis at | First evidence |
| --- | --- | --- |
| Cannot open [localhost:5678](http://localhost:5678) | `S2` | Compose status and n8n logs |
| No news enters the workflow | `S4` | Tavily item and result counts |
| Items disappear before Gemini | `S5` | Item count after every processing node |
| Gemini HTTP error | `S6` | Status code and redacted response |
| Code node JSON or results error | `S6–S7` | Gemini text, finish reason, and Code error |
| Google sign-in or send error | `S8` | OAuth/Gmail error text without secrets |
| Manual run works but daily run does not | `S9` | Published state, trigger connection, and production executions |
| Daily run occurs at the wrong time | `S9` | Compose time zone and schedule hour |

## Unknown Error Procedure

If an error is not listed:

1. Record the last verified checkpoint.
2. Identify the first command or node that failed.
3. Ask for the exact error text, node name, item count, HTTP status when present, and a redacted screenshot or output excerpt.
4. Check whether the failure is configuration, authentication, network, quota, empty data, or publishing state.
5. Give one corrective action and one verification action.
6. Advance the checkpoint only after the expected result appears.

## Completion Definition

The project is complete only when:

- Docker and n8n are running.
- Every node from Tavily through Gmail has a successful non-empty test execution.
- The recipient has received the HTML email.
- Manual Trigger remains available.
- Schedule Trigger is configured.
- The workflow is published.
- At least one scheduled production execution is confirmed when the user needs end-to-end automation verification.