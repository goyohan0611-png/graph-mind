# Packet 30 items / 15,000 chars vs shipped 20 / 10,000 — written before the run (2026-10-08)

Same 500 questions, same order (ids.json copied from graph-v6.9-full500-warm), same reader
(gpt-5-mini), same judge (gpt-4o-2024-08-06), same warm-cache spans procedure. Only
`--limit 30 --chars 15000` differs. Baseline: graph-v6.9-full500-warm (430/500).

Primary: paired exact McNemar on all 500. Secondary: multi-session (all and the 380 untuned),
every other type's net change, median packet tokens.

Adopt as the shipped default only if overall net > 0 with p < 0.05. Identical-config reruns
differ by ~9/120 answers, so a net gain without significance is reported as noise, not adopted.
If multi-session gains but overall does not, the option is a per-request size for counting/
aggregation questions, decided separately (not by this run).
