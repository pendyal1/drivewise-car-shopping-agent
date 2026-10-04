# DriveWise — compare the cars you already found

DriveWise is an IEOR 4570 Project 1 web agent for U.S. shoppers with a shortlist of two to five vehicle models or listings. Bring year, make, model, optional configuration/EPA ID, and any asking prices or mileage. DriveWise combines free government evidence with deterministic ownership costs and an explainable lifestyle comparison.

**It does not search live inventory, establish current local market value, inspect mechanical condition or title history, or prove recall repair completion.** Estimates are not lender, insurance, repair, or purchase guarantees. LiteLLM optional telemetry and remote pricing-map fetching are disabled. No non-GCP API key, commercial vehicle API, paid subscription, trial, or scraping is used. VINs are optional user-supplied context only; this implementation makes no VIN requests, individual or bulk.

Author: `ap4839@columbia.edu` · GCP project: `ieor-4570-f26-ap4839` · Cloud Run region: `us-central1`.

## Why this project is original

The central tool is a **lifestyle fit notebook**, not a generic recommendation prompt. A single call retrieves EPA efficiency and NHTSA evidence, models cost when the user supplies adequate inputs, and calculates six transparent components. It makes uncertainty visible through neutral missing-data scores, completeness, configuration options, scope notes, and separate hard-constraint failures. A commuter, street parker, and winter traveler can see exactly which preferences change a comparison. No LLM-generated hidden score decides the winner.

The custom cream-and-green frontend includes a clearly fictional lifestyle-ranking example, shortlist instructions, limitations, sample-query buttons, and expandable evidence cards. It uses plain HTML/CSS/JavaScript and local system fonts, with no framework, build pipeline, or external frontend dependency.

## Architecture and starter comparison

`index.html` → FastAPI `/chat` → LiteLLM → **`vertex_ai/gemini-3.5-flash-lite` on Vertex AI, `global`** → JSON tool dispatch → government HTTP + Python calculations → tool results → Gemini response.

The supplied `examples/gemini-web-tool-calling.zip` remains the architectural starting point: `app.py`, handwritten tool schemas, a five-round harness, assistant/tool history, and the response shape are retained. The generic weather tool and starter page are replaced by the DriveWise tools and purpose-built UI. The existing DriveWise cost and recall concepts are improved in place; this is not a separate replacement project. The class lecture's explicit schemas, combined internal sequences, bounded evidence, and actionable errors guide the implementation.

| File | Purpose |
| --- | --- |
| `app.py` | Gemini harness, typed API, safe errors, concurrency-safe bounded session store |
| `tools.py` | Five schemas, shared government HTTP/parsing helpers, normalization, cost and scoring logic |
| `index.html` | Responsive accessible conversation UI, sessionStorage, expandable tool evidence |
| `tests/` | Mocked government/model fixtures and focused pytest coverage |
| `pyproject.toml`, `uv.lock` | Runtime dependencies and dev test dependencies, locked with uv |
| `.gitignore`, `.gcloudignore` | Exclude local credentials, environments and caches |
| `submission.json` | Required author and deployment URL; placeholder must be replaced |

`POST /chat` accepts `message` (nonblank, at most 8,000 characters) and optional returned UUID `session_id`. Its required shape is unchanged:

```json
{
  "response": "...",
  "session_id": "...",
  "tool_calls": [
    {"name": "...", "args": {}, "result": "JSON string"}
  ]
}
```

`GET /` serves the frontend. `GET /health` returns `{"status":"ok"}` without contacting Vertex or government services; it is a process-health check, not a Gemini credential check. `POST /clear` requires a JSON body `{"session_id":"the-returned-UUID"}` and clears only that session. Missing/invalid input returns safe HTTP 422 JSON. Internal failures do not expose exception text, headers, URLs with query parameters, environment values, or debugging tracebacks in API responses.

## The five tools

All tool names are `snake_case`. Schemas explain call/no-call situations, argument units, required versus optional inputs, ranges, patterns, enums, and candidate limits; the dispatcher enforces them. Unknown tools, malformed arguments and unexpected failures return actionable JSON.

| Tool | Use and output | Boundary |
| --- | --- | --- |
| `get_vehicle_efficiency` | Year/make/model plus optional configuration or EPA vehicle ID. Completes year → make → model → options → record flow; normalizes MPG/MPGe, energy use, engine, transmission, drivetrain, class, annual government fuel cost, EV range/charging, available volumes and scores. | Multiple configurations return up to eight options and a selection request. No silent choice. EPA ID must match identity. Unavailable fields are explicit. |
| `calculate_ownership_cost` | Price, down payment, APR percentage and loan months; usage, energy and allowance inputs. Returns principal, payment, interest, monthly gasoline/diesel/electricity, insurance, maintenance and total. | Needs MPG for gasoline/diesel/hybrid, electric consumption for EV, and both modes plus electric-mile share for PHEV. No guessed MPG. |
| `check_vehicle_recalls` | NHTSA year/make/model campaigns: number, component, summary, consequence, remedy and date. Up to eight campaigns, 1,200 characters per text field, full returned count. | Model-level only: does not establish VIN inclusion, open repair status or completed repairs. Empty results are not a certification of safety. |
| `get_vehicle_safety_profile` | Internally retrieves NHTSA variants, then ratings by ID (up to four variants), plus recall count and bounded complaint context. | Common rating only when all variants agree and none are missing. Different variants remain visible. Missing ratings do not discard recall/complaint evidence. |
| `rank_vehicles_for_lifestyle` | Two to five supplied candidates and optional preferences. Internally retrieves efficiency/safety, estimates cost when possible, and returns ranking, points, weights, reasons, tradeoffs, constraints, missing data, completeness and sources. | No inventory search or price valuation. Missing data is neutral. Supplied VIN/mileage are retained without repair/reliability predictions. |

### Government sources and request safety

- [FuelEconomy.gov web-service documentation](https://www.fueleconomy.gov/feg/ws/index.shtml): official menu flow and vehicle records under `https://www.fueleconomy.gov/ws/rest`.
- [NHTSA datasets and API documentation](https://www.nhtsa.gov/nhtsa-datasets-and-apis): `/SafetyRatings/modelyear/...`, `/SafetyRatings/VehicleId/...`, `/recalls/recallsByVehicle`, `/complaints/complaintsByVehicle` at `https://api.nhtsa.gov`.

Only fixed HTTPS government endpoints are accepted. Requests use JSON `Accept`, a DriveWise user agent, 3-second connect and 6-second read timeouts, disabled redirects, a 1 MB response cap, and a 45-second government-request budget per dispatched tool. A call-local cache avoids duplicate requests within a comparison. Service delays can produce partial comparisons; retry fewer candidates. XML fallback uses the standard library with entity/DOCTYPE rejection and a depth limit. Government text is bounded before reaching the model/UI. Errors distinguish invalid input, timeout, rate limit, upstream authentication/rejected request/unavailable service, malformed response, and no match. No raw error bodies or exceptions are echoed.

EPA model names may include `Hybrid`, while NHTSA may use the base model. If an exact hybrid lookup is empty, the recall/safety tools may attempt the base name and **explicitly return the lookup identity and fallback scope**. Base-model safety applicability to the exact hybrid remains unconfirmed, so ranking leaves that safety component neutral. Neither empty exact results nor broader results prove configuration-specific safety. Matching variants and recall applicability must be verified before purchase.

NHTSA complaints are submitted reports, not necessarily verified defects. Counts are affected by sales volume and vehicle age and are not reliability scores. The tool exposes a count capped at 100 and at most five component labels sampled from the first 20 returned reports; it excludes individual complaint narratives and identifiers. If the complete response exceeds the byte cap, complaints are marked unavailable. Model recall counts never directly reduce the safety score.

### Ownership-cost model

Loan principal is `price − down_payment`. For monthly interest `r = APR / 1200` and term `n`, payment is `principal × r / (1 − (1+r)^(-n))`; at zero APR it is `principal / n`. Full down payment is allowed. Total interest is computed from the unrounded payment, so displayed monthly rounding can produce a small difference from multiplying the displayed amount.

Combustion energy cost is `annual miles / MPG × USD/gallon / 12`. EV cost is `annual miles × kWh/100 miles / 100 × USD/kWh / 12`. MPGe converts to `3370 / MPGe` kWh/100 miles using the EPA energy equivalence. PHEV estimates split annual miles using a **user-supplied electric-mile fraction**, applying electric consumption to those miles and gasoline-mode MPG to the remainder. This is an approximation; blended PHEV operation is outside the ranking cost model and is flagged as unavailable rather than guessed.

Declared defaults when omitted: 10,000 miles/year, $3.50/gallon, $0.16/kWh, $175/month insurance allowance and $75/month maintenance reserve. The standalone calculator defaults to gasoline; explicit powertrain input is recommended. These are shopping assumptions, not current local prices or quotes. Results separate `user_provided_inputs`, `government_source_data`, and `defaults_and_assumptions`. `energy_data_source=government_epa` is appropriate only for an actual EPA record already retrieved; otherwise the measure is a user estimate. Ranking assigns that provenance internally. EPA's annual fuel cost uses EPA assumptions; the user's monthly budget is calculated separately.

**Excluded:** taxes, registration, dealer fees, depreciation, repair predictions, resale value and future price changes. Ownership duration is retained as context but does not forecast those costs. Insurance and maintenance are allowances, not guarantees.

## Lifestyle methodology

Base points total 100:

| Component | Weight | Deterministic fit fraction (0 to 1) |
| --- | ---: | --- |
| Affordability | 30 | Mean of available budget ratios: `clip(1 − cost / target / 2)`, using purchase price/firm budget and estimated monthly cost/desired target. At target: midpoint. No adequate comparable inputs: midpoint. |
| Energy | 20 | Combustion/PHEV gasoline-mode MPG: `clip((MPG−15)/45)`; EV MPGe: `clip((MPGe−15)/125)`. If city share and both MPG values exist, use the harmonic blend `1/(city share/city MPG + highway share/highway MPG)`. Fuel preference match is averaged with this fraction. Unwillingness to charge gives EV zero and caps PHEV at 0.25. |
| Space/class | 15 | Mean of available requested proxies: passenger volume/(occupants × 25 cu ft), cargo volume/10, 18 or 30 cu ft for low/medium/high cargo, clipped to 1. Parking class fit: compact 1, large/standard/van 0.25, other 0.5. |
| Weather/drivetrain | 10 | When winter need and drive data exist: AWD/4WD 1, FWD 0.6, other drivetrain 0.4. Otherwise midpoint. Tires and driving conditions still matter. |
| Safety | 15 | Common, applicable overall NHTSA stars: `(stars−1)/4`. Missing, variant disagreement or unconfirmed hybrid fallback: midpoint. No recall/complaint penalty. |
| Environment | 10 | Available EPA GHG score/10. Missing: midpoint. Not a full lifecycle-emissions estimate. |

Optional fuel-efficiency, safety and environmental priorities multiply their respective base weight by 0.5/1/1.5 for low/medium/high, then renormalize all weights to total 100. Every result returns the final weights. Omitted preferences do not become guessed preferences.

Each missing component receives **half of its available weight**, never zero. Completeness is the fraction of six components supported by usable evidence and preferences; it is not certainty, reliability, or a percentage of all possible vehicle data. Unavailable partial inputs are described separately. Highest total score wins ties by higher completeness, then original candidate order. Reported scores are rounded to three decimals.

Hard constraints are separate: listing price over a firm purchase budget, or an EV when the user is unwilling to charge. Monthly target is explicitly a desired soft target. A high-scoring vehicle can still violate a hard constraint; read the constraint column before considering it. No constraint is established from missing price/cost data. The system prompt requires explaining these failures. No ranking claims objectively best, mechanically sound, reliable, fairly priced or guaranteed affordable.

Passenger volume cannot establish seating capacity or child-seat fit, and EPA class cannot establish dimensions/turning radius. Winter drivetrain fit cannot guarantee traction. These are explicit rough lifestyle proxies, useful for discussion and requiring physical verification.

## Sessions and privacy

The server stores separate UUID sessions in memory, with global bookkeeping and per-session locks. Defaults: 30 minutes inactivity, at most 100 sessions, at most 120 retained messages. Active conversations are not evicted. Idle sessions expire or the oldest idle session is evicted at capacity; all-active capacity returns 503. Old **whole user turns** are removed at the message limit, preserving assistant tool calls with their responses. An oversized single turn is discarded together rather than leaving orphaned tool messages.

The UI saves only `session_id` in `sessionStorage`, restoring context after refresh within the same browser tab. The visible transcript is not persisted; the server remembers retained context. New conversation clears only the active server session and removes its tab-storage entry. If clearing fails, the existing session is kept for a retry. Session IDs are opaque bearer handles: do not share them. User messages and selected normalized tool evidence are sent to Vertex AI for the answer; optional VINs should be included only if the user wants them in that context.

Cloud Run restarts, deployments and session eviction clear history. Configure **one instance maximum and one uvicorn worker** for this class deployment to reduce cross-instance loss. Even then, revision changes can lose context. No database or persistent paid service is added. The class's IAP restriction is the deployment access boundary; the local development server itself has no account authentication.

## Local setup and GCP authentication

Install Python 3.10+ and [uv](https://docs.astral.sh/uv/), then run from this existing project directory:

```bash
gcloud auth login ap4839@columbia.edu
gcloud config set account ap4839@columbia.edu
gcloud auth application-default login --account=ap4839@columbia.edu
gcloud config set project ieor-4570-f26-ap4839
gcloud auth list
gcloud config list account project
export GOOGLE_CLOUD_PROJECT=ieor-4570-f26-ap4839
uv sync --locked
uv run app.py
```

Open `http://localhost:8000`. Direct execution binds `0.0.0.0` and honors `PORT` (default 8000). Google credentials remain in the gcloud-managed ADC store; do not copy them into this repository or create a service-account key. `.env` is not needed or automatically loaded. The Google project setting is not a secret; no `.env.example` is necessary. `.env`, `.venv`, credentials and caches are ignored.

Use the education billing account and APIs in the supplied **Setting up GCP and Gemini** and **Deploying to Cloud Run from GitHub** guides. Vertex/Agent Platform uses service name `aiplatform.googleapis.com`. Cloud Run's service region is `us-central1`, while this Gemini model's location remains `global`.

## Tests and verification

```bash
uv sync --locked
uv run pytest
uv run python -m compileall -q app.py tools.py
```

Tests block all live requests by default. Government requests and LiteLLM completions are mocked; no test calls Gemini or charges a model. Coverage includes financing/zero APR/input rejection/all powertrains/provenance; EPA normalization/configuration ambiguity/XML/unavailable fields; NHTSA recalls/variants/missing ratings/complaint cautions; deterministic ranking, neutrality, constraints and ties; safe timeout/status/parsing/secret handling; exact `/chat` shape/history, continuity, isolation, clearing, session expiry/capacity/retention/concurrency; and `/health` and `/`.

Implementation verification on October 3, 2026: `uv sync --locked` passed; `uv run pytest` reported **124 passed, 2 warnings in 5.84s** (dependency deprecation warnings in the test client); compilation passed. A local server returned HTTP 200 for `/` and `/health`. Headless Chromium checked desktop/mobile layout, tool-card disclosure, safe rendering/links, disabled duplicate sends, refresh/session clearing and safe HTTP/malformed-body errors with zero page errors. Browser chat responses and all test model/government requests were mocked. The in-app Browser connection failed due to a tooling configuration error, so local Chromium was used. An ADC file exists, but its validity and real Gemini invocation were not tested.

Local health and frontend checks:

```bash
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/ -o /tmp/drivewise-index.html
```

A real Gemini smoke test still requires working ADC, model access, billing and IAM. Passing mocked tests or `/health` alone does not verify those credentials or upstream availability.

When changing dependencies, run `uv lock` and repeat locked sync/tests. No Dockerfile is required by the class buildpack workflow.

## Exactly three primary grader conversations

Run these sequentially in the **same tab/session**, answering configuration clarification questions as needed.

1. **Lifestyle comparison**

   > Compare a 2022 Honda Accord Hybrid, a 2022 Toyota Camry Hybrid, and a 2021 Subaru Legacy. I drive 15,000 miles a year, have a 40-mile daily commute, park on the street, occasionally drive in snow, and want to keep total monthly cost below $750.

   Expected: `rank_vehicles_for_lifestyle` retrieves EPA and NHTSA data internally. It shows scores, weights, tradeoffs, ambiguity and completeness. Asking prices and financing were not supplied, so monthly ownership cost cannot yet be established; affordability stays neutral. Ask for configuration selection where EPA is ambiguous, and explain any NHTSA base-model fallback. Do not invent a price or claim the $750 target is satisfied.

2. **Follow-up memory and affordability**

   > Use the best fit from that comparison. Assume a $24,000 purchase price, $5,000 down, 6.5% APR for 60 months, and $175 per month for insurance. Show the monthly estimate and your assumptions.

   Expected: remembers the selected candidate and 15,000 annual miles. If the previous ranking lacked adequate evidence, clarify which candidate/configuration to use. Call `get_vehicle_efficiency` only if usable efficiency is missing; then `calculate_ownership_cost` with the selected powertrain and actual energy data, tagged as government provenance. Explain the fuel/electricity-price and maintenance defaults and exclusions. A listing price here is a user assumption, not verified market value.

3. **Safety and recalls**

   > What NHTSA safety ratings and recall campaigns are available for that same vehicle, and what do those results not tell me?

   Expected: remembers the same year/make/model. Call `get_vehicle_safety_profile` for ratings/variant and complaint context and `check_vehicle_recalls` for campaign details. Explain missing/ambiguous ratings, lookup scope and base-model fallback where relevant, the complaint-count cautions, and why model-level campaigns do not establish VIN inclusion or repair completion.

## Remaining continuous-deployment steps — do not treat as completed

Follow the supplied class guide's **GitHub → Developer Connect → Cloud Build → Cloud Run** route. These steps create resources/change permissions and must be performed by the account owner or separately authorized. No push, cloud creation, IAM change or deployment was performed for this implementation.

1. This local directory initially had **no Git repository**. Create a dedicated GitHub repository, then initialize/commit these existing files and push a `main` branch. Keep `app.py`, `pyproject.toml`, and `uv.lock` at the repository root; do not nest them beneath an extra folder. Check exclusions before staging. For example, after replacing the URL with your actual repository:

   ```bash
   git init -b main
   git add app.py tools.py index.html pyproject.toml uv.lock README.md submission.json .gitignore .gcloudignore tests
   git commit -m "Implement DriveWise government-data comparison agent"
   git remote add origin https://github.com/YOUR_GITHUB_USERNAME/drivewise-car-shopping-agent.git
   git push -u origin main
   ```

2. Confirm the correct Google account/project and education billing account. Enable Vertex AI, Cloud Run, Cloud Build, Artifact Registry, Developer Connect, Secret Manager (the guide requires this **before** GitHub linking), and IAP. The command below is for the owner to run, not evidence of resources already enabled:

   ```bash
   gcloud services enable aiplatform.googleapis.com run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com developerconnect.googleapis.com secretmanager.googleapis.com iap.googleapis.com --project=ieor-4570-f26-ap4839
   ```

3. In Cloud Run for project `ieor-4570-f26-ap4839`, choose **Connect repository → Continuous deploy from a repository → Developer Connect → Set up with Developer Connect**. Link the GitHub repository, install/authorize the GitHub app for this repository, and select the repository and `main` push trigger (`^main$` if a branch regex is requested). Use `us-central1` as the service region. A Developer Connect connection/build may have its own region; retain it for troubleshooting.

4. Choose **buildpacks**, source directory `/` (repository root), and set the entrypoint exactly to:

   ```text
   uvicorn app:app --host 0.0.0.0 --port $PORT
   ```

   Keep one worker. No Dockerfile. Configure `GOOGLE_CLOUD_PROJECT=ieor-4570-f26-ap4839`, maximum instances **1**, minimum instances **0**, and a request timeout sufficient for government calls and the bounded model loop (for example **900 seconds**); 1 GiB memory is a reasonable starting setting for this Python dependency set and session cache. The repository uses locked uv dependencies; buildpack dependency detection must succeed in the first build.

5. Select the runtime service account and grant that account permission to invoke Vertex AI in this project, normally **Vertex AI User (`roles/aiplatform.user`)**. Do this in the console IAM page with the account actually assigned to this service. Use platform-provided credentials, **not a downloaded key file**. The build/deployment identities also need the permissions required by the console workflow; follow its prompts rather than granting broad Owner roles or assuming the runtime and build identities are the same.

6. Per the class guide, choose **Require authentication → Identity-Aware Proxy (IAP)**. Edit policy and set the principal to **`columbia.edu`** (it may not autocomplete). Save; do not make the service publicly unauthenticated. Ensure the grader's Columbia account can open the app.

7. Create the service and wait for a successful Cloud Build and healthy Cloud Run revision. Open the real service URL through Columbia sign-in. Verify `/`, `/health`, tool cards, the three conversations, same-tab refresh continuity and New conversation. A successful build does not prove Vertex invocation; run an actual grader conversation. Verify a subsequent authorized `main` push triggers and deploys a new revision.

8. Replace `submission.json`'s **`REPLACE_WITH_REAL_CLOUD_RUN_URL_BEFORE_SUBMISSION`** with the actual HTTPS service URL, retaining `authors: ["ap4839@columbia.edu"]`. Commit/push that update through the same workflow. Keep the service running until grades are released.

**Submission blocker:** the deployment URL is still a placeholder. GitHub repository creation/linking, IAM, cloud build/deployment, real Gemini/government-service smoke testing, and grader access remain to be verified externally.

## Troubleshooting

- **Gemini unavailable:** verify ADC with the setup commands, project and education billing, Vertex API, model access, and runtime `roles/aiplatform.user`. Keep `global` for the prescribed model. The UI intentionally hides exception details; inspect private Cloud Run logs without copying credentials into reports.
- **EPA ambiguity/no match:** use an exact EPA model name (often including Hybrid), choose an option or matching vehicle ID. Do not treat a trim label as proof of an EPA configuration. Efficiency is an EPA test estimate, not real-world MPG.
- **No NHTSA rating:** a vehicle/variant may not have been rated; use missing evidence and neutral scoring. Check the returned lookup identity. Broader hybrid fallback is not proof of exact configuration applicability.
- **Government timeout/429/unavailable:** wait and retry a narrower comparison. Partial evidence and missing-data notes are expected. Do not substitute guessed results.
- **Lost context:** inactivity, eviction, app restart, deployment or another revision can reset memory. Keep one Cloud Run instance and worker; provide the shortlist again when the UI reports a changed session.
- **First build: `developerconnect.gitRepositoryLinks.fetchReadToken` denied:** the class guide says new connection permissions may still be provisioning. Wait a minute, then Cloud Build → History → failed build → Rebuild.
- **Build missing from History:** select the Developer Connect connection's build region, which may differ from the Cloud Run service region.
- **Startup/build failure:** confirm repo-root files, locked dependencies, the buildpack entrypoint, `$PORT` and `0.0.0.0`. Never add local `.venv` or credentials to fix a build.
- **IAP access denied:** verify Columbia sign-in and the guide's `columbia.edu` policy with the owner. Do not disable authentication to bypass it.

## Assignment and peer-review alignment

Assignment: session memory and isolation; five designed tools; external government requests; original deterministic lifestyle tool; safe failures; exact `/chat` contract; visible tool cards; distinct frontend; three grader conversations; required root files; locked tests; Cloud Run buildpack/CD instructions. Deployment itself and the real URL remain unfinished submission requirements.

Peer review: **Experience and usefulness** — a practical shortlist flow, follow-ups, explicit budgets and visible evidence. **Risk-taking and ambition** — integrated government menu/variant sequences, multiple powertrain cost models and deterministic multi-component comparison. **Originality and voice** — lifestyle proxies, neutral uncertainty handling, transparent scoring and a custom decision-notebook interface. These are design strengths for reviewers to assess, not claims of a guaranteed rubric score. Live usefulness still depends on service data availability and deployment validation.
