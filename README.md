# Vera Challenge Bot

Deterministic, rules-based assistant for the Magicpin Vera challenge. It uses no external model/API, so identical contexts and state produce identical output without network dependency.

## Design

`bot.py` exposes the five documented endpoints and the reusable `compose(category, merchant, trigger, customer)` function. The composer first chooses a supported action from the trigger and its evidence, then renders a category-specific message for dentists, salons, restaurants, gyms, or pharmacies. It cites supplied research, selects only active merchant offers, and avoids unsupported claims. Pharmacy copy defers prescription decisions to a pharmacist; dental, fitness, and salon copy avoids outcome guarantees.

When triggers compete, the bot uses a fixed ordinal priority: safety/compliance and explicit planning first, then due customer actions, performance/renewal issues, review and market opportunities, then curiosity/dormancy. The supplied urgency, expiry proximity, and metric magnitude break ties. It sends at most one action per merchant per tick, checks expiry and consent, and suppresses repeated keys, duplicate ticks/replies, repeated message bodies, opt-outs, and sends inside a four-hour audience cooldown. Merchant and customer opt-outs are tracked separately. Sparse or unsupported contexts are skipped rather than filled with invented data.

Context versions replace older versions atomically. Context, delivery, suppression, and conversation state are process-local in-memory stores, protected by a lock; they survive requests but not process restarts and are intended for the challenge's single-instance test. A multi-instance deployment needs a shared transactional store and durable opt-out/suppression records. `/v1/teardown` clears all state.

The reply handler classifies acceptance, rejection/opt-out, canned replies, questions, objections, acknowledgments, and off-topic requests. It proceeds on acceptance, backs off on auto-replies, and ends on explicit rejection. Since the API has no publishing or messaging tools, replies never claim to have changed a profile or contacted customers.

## Run and Test

Python 3.10+ with FastAPI and Uvicorn is required. From the repository directory:

```powershell
python -m pip install fastapi uvicorn
uvicorn bot:app --host 0.0.0.0 --port 8080
```

In another terminal:

```powershell
python -m unittest -v test_bot
python dataset/generate_dataset.py --seed-dir ./dataset --out ./expanded
python generate_submission.py --dataset ./expanded --output ./submission.jsonl
```

No API key or environment variable is required by the bot. Optional metadata variables: `TEAM_NAME`, comma-separated `TEAM_MEMBERS`, `CONTACT_EMAIL`, and `SUBMITTED_AT`. The Render Blueprint is `render.yaml`; connect a private GitHub repository containing the app files to Render and create a Blueprint deployment. `judge_simulator.py` is excluded from Git because it contains a local scorer key; the cloud app does not need it. Use one application instance and do not use multiple workers with the in-memory state implementation. The service uses HTTPS at its Render URL and `/v1/healthz` as its health check. A provider account and authenticated repository connection are required to create the public URL.

## Tradeoffs

Fixed rules are auditable and low-latency, but less fluent than a well-constrained LLM and cannot infer missing facts. The supplied simulator's rubric scorer requires a separately configured LLM provider/key; local functional tests do not claim an official LLM score. The generator's correct invocation includes `--seed-dir ./dataset` because its default expects seed files at the current directory.