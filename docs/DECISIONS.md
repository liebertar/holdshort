# Decisions

Short records of choices that shaped the system, with the reason. Newest last.

**No model in the judgement path.** Aviation already settled this: automation with authority is
deterministic, advisory tools may use whatever they like. A refusal must be reproducible from the ledger.

**The runtime verifies; it never plans.** Route choice, dispatch, budgets and battery belong to the
operator. If the runtime planned routes it would own liability for them and stop being an authority.

**Two worlds from one seed, never rigged.** The direct wiring runs the same agent code against the same
detector and form writer; it merely has nowhere to file. A demo that sabotages the control loses the argument.

**Buildings come from the tiles the screen draws.** The judge and the map must see the same shapes, or the
picture lies. Threshold 20 m, because the first version knew only 40 m+ and approved 40 m legs over 35 m roofs.

**70 m cruise floor, bent under low cells.** A fixed 70 m floor made the whole of upper Manhattan
unreachable (200 ft cells). Under such a cell the floor is the ceiling and low roofs get 20 m; tall roofs keep
50 m, which forces a lateral detour.

**Free routing, not sky lanes.** Fixed corridors would make separation trivial and the agents pointless.
Cleared routes become temporary reserved volumes instead.

**Tighten now, loosen with a person.** A rule that closes airspace applies the tick it arrives; a rule that
opens it waits for a human or an expiry. Model-read prose is always held for a person before it applies.

**Lost link: continue and land, never return.** A return path is an uncleared path, and if the runtime itself
fails every aircraft would turn at once into each other. Finishing the cleared route keeps the deconfliction
that already exists. The runtime's job is to keep the dark aircraft's space reserved.

**Nemotron for forms, prose and summaries; A\* for geometry.** Measured, not assumed: local drafts clear the
judge only on short routes. The map says who drew each corridor so this is visible, not hidden.

**Budgets are off by default.** Money caps put a healthy aircraft into a human queue mid-round. Spending is
the operator's concern; the runtime's authority is physical. The authority check still enforces a cap if
`configs/fleet.yaml` sets one.

**Roof bays 31 m apart.** Designated, visible, one per aircraft. 22 m tripped the 30 m parked-aircraft rule;
31 m clears it while takeoff columns still overlap, so simultaneous departures serialise — the right picture.

**One round is 5,000 ticks.** A Harlem round trip under the strict rules takes about 4,040 ticks.

**Intake sources are trusted by origin, not by parser.** A METAR from the aviation weather service applies at
once; a search snippet or a manual post waits for a person even when the grammar read it perfectly.

**SQLite for the intake store, on the same volume as the ledger.** Same availability needs, no new service,
swappable behind a thin interface if a deployment wants Postgres.

**Name.** sky-net is the net over a shared sky that every operator's drones fly inside. Spelled apart from
the film's Skynet on purpose: this one cannot move anything on its own. Agents propose; code clears.

**The model chooses among the planner's routes; it does not draw them.** A 4B model writing waypoints rarely
cleared the judge on long crossings. Finding a path between buildings is a search, and A* does it. Choosing
among routes that are already legal, given the weather, the closed cells and the other aircraft's windows, is
a judgement, and calling a tool is what the Nemotron cards describe. Drafts stay as the last resort after
every candidate is refused. The runtime judges a pick exactly as it judges any other route.

**PX4 is a mirror, not the world of record.** The simulator decides positions, cargo and the scoreboards for
all four aircraft; PX4 flies one of them from the same commands, and its answers are written beside the
ledger (`AUTOPILOT_LOG`, `/state.autopilots`) without ever changing a verdict. The two-world comparison needs
one world scoring both wirings, and PX4 SIH keeps lockstep only at 1× on this Mac's Docker while the round
runs at 4×. What PX4 proves is the command path: the cleared route arrives as a real mission, a recall
reaches a real autopilot, a refused filing never arms it.

**Recorded briefing is always labelled.** Without a Tavily key the briefing replays hand-written fixtures in
Tavily's response shape. The map, the ledger and the intake store all say "recorded", and recorded items carry
their own id prefix, so a recorded scene cannot pass for a live answer on the day a key is added.

**Zone dwell counts only what a wiring could have avoided.** No aircraft leaves a zone the tick it closes. An
aircraft inside at closure is allowed the time to the nearest exit at cruise from anywhere in the zone
(22 ticks); every tick after that counts, and so does every tick of an aircraft that entered after closure.
Both wirings are scored the same way. The difference is that one of them can be told to leave.

**The runtime gets its own model server.** The Ollama app loads a 262,144-token context, which puts over 5 GB
more KV cache on the same 4B model, while a notice is about 2,000 characters. `scripts/ollama_fleet.sh` starts
a runtime server on 11439 with an 8k context, and the resolver prefers it to 11434.

**One folder per stack.** `frontend`, `backend`, `drone`, `shared` and `sim`, each with its own Dockerfile.
What would ship to an aircraft and what runs at the authority are separate images, and the guarded drone
image is built without the runtime, the adapters or the direct wiring, so nothing in it can reach a motor.

**The map shows the runtime's world only.** The direct wiring is the comparison, not the product. It runs as an
optional overlay and in the seeded harness for the scoreboard; the map draws only what the runtime clears.

**The runtime is drawn at 26 Federal Plaza.** A clearance service shared by every operator belongs to no
operator's depot. The map places it on a federal building in Lower Manhattan, with its links at 600 m so
they clear the skyline.
