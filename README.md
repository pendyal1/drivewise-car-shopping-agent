# DriveWise

Car shopping gets complicated quickly. The cheapest listing is not always the cheapest car to own, and the vehicle with the best fuel economy may not fit someone who deals with snow, street parking, a long commute, or limited charging access.

DriveWise compares vehicles against the way a person actually drives. It combines FuelEconomy.gov records, NHTSA data, financing math, and a transparent lifestyle-fit score. A user can bring two to five cars, describe what matters, and see the tradeoffs in one conversation.

Exact trims are helpful, but they are not required. DriveWise can match details such as fuel type, drivetrain, engine, and transmission to government configurations. When those details are missing, it chooses a representative configuration, reports its confidence, and shows alternatives that could change the result.

## What DriveWise can help with

DriveWise is meant for comparing a shortlist, estimating costs, and understanding available government data. It can:

- Compare two to five vehicles for a specific driver.
- Consider annual mileage, commute length, city and highway driving, passengers, cargo, parking, charging access, weather, and road conditions.
- Resolve likely FuelEconomy.gov configurations when the exact trim is unknown.
- Estimate monthly loan, fuel or electricity, insurance, maintenance, and total ownership costs.
- Retrieve EPA fuel economy, energy use, annual fuel cost, vehicle class, and available range or charging information.
- Retrieve NHTSA safety ratings, model-level recall campaigns, and bounded complaint information.
- Explain which needs favor one vehicle and which missing facts could change the ranking.

The app does not search live inventory or decide whether a listing is fairly priced. It also cannot inspect an individual car. Users provide the vehicles and any listing prices or mileage they want included.

## Try these questions

### 1. Compare a shortlist

> Compare a 2022 Honda Accord Hybrid, a 2022 Toyota Camry Hybrid, and a 2021 Subaru Legacy. I drive 15,000 miles a year, commute 40 miles a day, park on the street, carry four people regularly, and deal with heavy rain, occasional snow, ice, potholes, and extreme summer heat. I do not have home charging. Automatically choose configurations when I do not provide trims.

DriveWise researches all three vehicles together, discloses the selected configurations, and explains what affects the order.

### 2. Continue with an ownership estimate

> Use the best fit from that comparison. Assume it costs $24,000, I put $5,000 down, finance it for 60 months at 6.5%, and pay $175 a month for insurance. What would my estimated monthly cost be?

This reuses the vehicle and annual mileage from the same conversation, then separates user inputs, government data, and assumptions.

### 3. Review safety and recalls

> Check the NHTSA safety ratings and recall campaigns for that same vehicle. Explain what those results tell me and what they do not.

DriveWise retrieves the available ratings and campaigns, then explains variant ambiguity and the limits of model-level recall data.

## How it works

1. The user describes the vehicles and the needs that matter to them.
2. Gemini decides which DriveWise tool is relevant.
3. The tool retrieves government data or performs the calculation in Python.
4. DriveWise keeps the conversation context and explains the result, assumptions, unknowns, and tradeoffs.
5. The interface shows each tool call in a collapsible card, including its arguments, a short summary, configuration confidence, and government source links.

Conversation context is separated by `session_id`. The browser keeps that ID in `sessionStorage`, so a refresh in the same tab can continue the conversation. **New conversation** clears the active session.

## Tools

| Tool | What it does | Source |
|---|---|---|
| `get_vehicle_efficiency` | Resolves a vehicle configuration and returns normalized MPG or MPGe, energy use, annual fuel cost, drivetrain, class, and available volume, range, and charging fields | FuelEconomy.gov |
| `calculate_ownership_cost` | Calculates financing, loan interest, monthly fuel or electricity, insurance allowance, maintenance reserve, and estimated monthly total | Local calculations, with optional EPA energy data |
| `check_vehicle_recalls` | Returns bounded model-level recall campaign details, including component, summary, consequence, remedy, and date | NHTSA |
| `get_vehicle_safety_profile` | Retrieves available crash ratings by vehicle variant, recall count, and bounded complaint context | NHTSA |
| `rank_vehicles_for_lifestyle` | Compares two to five vehicles against the driver’s budget, commute, weather, parking, space, road, and charging needs | FuelEconomy.gov, NHTSA, and local calculations |

Configuration selection follows a fixed order. A valid `configuration_id` wins. Otherwise, DriveWise matches trim, fuel, drivetrain, engine, transmission, and body-style hints. With no useful hints, it chooses a representative near the middle of the efficiency and fuel-cost range. It reports confidence, alternatives, and the possible range without claiming the representative is the most popular trim.

## Data sources

[FuelEconomy.gov](https://www.fueleconomy.gov/feg/ws/index.shtml) supplies EPA vehicle records, including available MPG or MPGe, electric consumption, annual fuel cost, drivetrain, transmission, class, interior volume, EV range, charging time, and emissions-related scores.

[NHTSA](https://www.nhtsa.gov/nhtsa-datasets-and-apis) supplies safety-rating variants, model-level recall campaigns, and consumer complaint reports. DriveWise keeps recall output bounded and does not expose complaint narratives.

Government coverage varies, and model names can differ between EPA and NHTSA. A model-level recall does not say whether a particular VIN is included or repaired. Complaints are user-submitted reports rather than verified defects, and counts reflect vehicle age and sales volume. DriveWise does not use a paid vehicle-data service.

## Running it locally

DriveWise requires Python 3.10 or newer and [uv](https://docs.astral.sh/uv/). From the repository root, run:

```bash
uv sync --locked
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-google-cloud-project-id
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000`.

The government APIs do not require separate API keys. Local Gemini access uses Google application-default credentials and the configured Google Cloud project.

## Testing

```bash
uv run pytest
uv run python -m compileall -q app.py tools.py
```

The tests mock Gemini and external HTTP requests. They cover cost calculations, government-response parsing, configuration selection, lifestyle ranking, weather and road handling, safe errors, session separation, and the `/chat` response shape. The latest verified local run completed with **138 passing tests**.

## Deployment

DriveWise is intended to run on Google Cloud Run with Vertex AI. Set `GOOGLE_CLOUD_PROJECT` to your Vertex AI-enabled project. Deployment uses continuous delivery from the GitHub repository through Google Cloud Build.

Enable the Vertex AI, Cloud Run, Cloud Build, Artifact Registry, Developer Connect, Secret Manager, and Identity-Aware Proxy APIs. The runtime service account normally needs `roles/aiplatform.user`. Configure the service to run:

```text
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Sessions are currently stored in process memory, so this version should use a maximum of one Cloud Run instance. A restart or new revision can still clear active conversations.

## Honest limitations

DriveWise does not search live listings or determine current local market value. It does not inspect mechanical condition, title history, accidents, tires, prior flood exposure, or maintenance records. Vehicle class and drivetrain can support limited comparisons, but they do not establish ground clearance, snow-tire quality, braking distance, reliability, or off-road ability.

Ownership costs are estimates. Financing, insurance, energy prices, maintenance, taxes, fees, depreciation, and repairs can differ from the assumptions used here. In-memory conversations disappear after a restart or session expiration.

DriveWise can organize the available information and make tradeoffs easier to see. It should support a shopping decision, not replace a pre-purchase inspection, a vehicle-history report, or verification of an individual VIN and recall-repair status.
