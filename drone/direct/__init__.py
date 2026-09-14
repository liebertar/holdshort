"""How most fleets are wired today: the agent holds the actuator address itself.

This is a separate package on purpose. It is not part of the guarded agent's container
image, and it does not use `backend.adapters` — a team that wires an agent straight to the
vehicle writes its own client, which is exactly why there is no shared place to put a rule.

It shares `drone.agent`'s eyes and hands: the same detector, the same proposal writer,
the same model. Only the wiring differs.
"""
