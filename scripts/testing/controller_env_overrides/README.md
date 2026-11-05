# Controller environment arms

`current_state_integrated.env` is applied to every campaign. Every other file is one
treatment arm or overlay, selected with `OSKEN_ENV_OVERRIDE_FILE`: one base file per
run, plus at most one overlay where a run needs one.

| Use this file | For |
|---|---|
| `current_state_integrated.env` | Every campaign — the thresholds, cooldowns and caps the arms are layered on |
| `rq1_event_preserving.env` | RQ1 reference — every summary delivered, in order |
| `rq1_delayed.env` | RQ1 — complete summaries delivered 30 s late |
| `rq1_sampled_push.env` | RQ1 — one summary in three delivered on time |
| `rq1_latest_state.env` | RQ1 — the consumer polls the latest summary every 30 s |
| `rq2v4_mode_none.env` | RQ2 reference — no tier may scale |
| `rq2v4_mode_comp.env` | RQ2 — only compute may scale |
| `rq2v4_mode_stor.env` | RQ2 — only storage may scale |
| `rq2v4_mode_both.env` | RQ2 — both tiers may scale |
| `rq2v4_p4_pins.env` | RQ2 compute-bound press, with a mode arm — the defaults those runs were launched with |
| `rq2v5_pins.env` | RQ2 mixed and data-access presses, with a mode arm — pool renewal, selection spread, 384 MB edge seat |
| `rq3sat_direct.env` | RQ3 admission modes — admits on the readiness message, compute cap 12 |
| `rq3sat_discovery.env` | RQ3 admission modes — admits on a successful probe, compute cap 12 |
| `rq3snd_wake_verify.env` | RQ3 admission modes, over `rq3sat_direct.env` — the message wakes a probe and admission waits for it |
| `rq3_direct.env` | RQ3 supporting sets — direct admission, compute cap 6 |
| `rq3_discovery.env` | RQ3 supporting sets — probe admission, compute cap 6, every 10 s |
| `rq3_discovery_15.env` | RQ3 poll-interval set — probe admission, compute cap 6, every 15 s |
| `rq3rob_event_only.env` | RQ3 fault overlay — a fallback check that falls past the episode |
| `rq3rob_loss_all.env` | RQ3 fault overlay — every readiness message dropped |
| `rq3snd_premature_lead10.env` | RQ3 fault overlay — the message arrives 10 s before the backend can serve |

The three fault overlays are merged with an admission arm rather than used alone, as in
`OSKEN_ENV_OVERRIDE_FILE=...rq3sat_direct.env` plus `...rq3snd_premature_lead10.env`.

The four RQ2 mode arms and both pins files run the `dual` selection policy, so they
differ only in the per-tier caps: a tier whose cap is 0 cannot act, and the other scales
on its own condition. `policy_gate.py` also offers a `bottleneck_aware` policy that
classifies the bottleneck and commits to the leading tier; no arm uses it.