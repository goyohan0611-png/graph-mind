# Benchmarks

Every number here comes from files in this repository: the pre-registrations, each question's
answer and the judge's verdict on it, under [`research/runs/`](../research/runs). The method is in
[REPORT.md](../research/REPORT.md), including the failures and the corrections.

## LongMemEval_S, answer accuracy, 500 questions

The answer model is gpt-5-mini; the judge is the official gpt-4o-2024-08-06.

| | accuracy | model tokens at write time | packet read per question |
|---|---|---|---|
| **All 500 questions** | **88.8%** (444/500) | **0** | 3.4k tokens |
| The 380 never used for tuning | **87.6%** (333/380) | 0 | 3.4k tokens |

Measured on 0.9.10's embedding model, the int8 export of multilingual mpnet
([`graph-v9.14-full500-mpnet8`](../research/runs/graph-v9.14-full500-mpnet8)). The earlier
model (multilingual MiniLM) scored the same 444/500 and 330/380 on the untuned questions. One
question gets no parsable answer (the reader runs out of output) and counts as wrong.

## Head to head

Same 40 questions, same answer model, same prompt, same official judge, pre-registered with code
hashes before anyone looked; nobody had tuned on those questions.

| | accuracy | model tokens spent saving memory, per question |
|---|---|---|
| **Graph-MIND** | **85.0%** | **0** |
| MemPalace 3.10.0 | 57.5% | 0 |
| Mem0 2.2.1 (open source) | 52.5% | ~640,000 |

Graph-MIND ran its shipped path with the earlier 20-item packet; Mem0 2.2.1 was the latest
open-source release on PyPI. The lead over both is significant (exact McNemar p = 0.002 and
0.003); with 40 questions each score has an interval of about ±14 points.

## agentmemory, all 499 questions

agentmemory publishes 95.2% on LongMemEval_S and states it is retrieval recall (recall_any@5),
not answer accuracy; it also publishes the sessions its hybrid search returned for each question.
Its first 5 sessions, every turn in full, went to the same answer model, prompt and official
judge as the 88.8% run. Only the retrieval differs. Its results file is pinned by commit and
hash in [`research/rival_agentmemory.py`](../research/rival_agentmemory.py); one question failed
to get an answer on retry and is left out of both columns.

| | accuracy | never used for tuning (379) | text the answer model reads |
|---|---|---|---|
| **Graph-MIND** | **89.0%** (444/499) | **87.1%** (330/379) | 3.4k tokens |
| agentmemory | 80.4% (401/499) | 78.6% (298/379) | ~16.5k tokens (66k characters) |

Graph-MIND alone was right on 64 questions, agentmemory alone on 21 (exact McNemar
p = 3.3e-6). By type, Graph-MIND / agentmemory: multi-session 80.8 / 65.8, temporal reasoning
88.9 / 77.0, single-session user 98.4 / 87.5, knowledge update 94.4 / 90.3, preference
75.0 / 71.4, abstention 93.3 / 90.0, single-session assistant 94.6 / 96.4.
Five whole sessions are agentmemory's unit of retrieval and its best case here: the answer model
reads about five times what Graph-MIND gives it. Per-question files:
[`research/runs/graph-v9.6-rival-agentmemory-k5`](../research/runs/graph-v9.6-rival-agentmemory-k5).

The retrieval number itself, on agentmemory's terms (is an answer session among the first K
retrieved?), from `bench/evidence.py` in CI. Graph-MIND retrieves turns, not sessions; its
sessions are counted in the order their first turn ranks. Its 479 questions are those with an
answer-bearing turn; agentmemory's 500 include the abstention questions, and on its 470
non-abstention ones it scores 95.1% / 98.5%.

| | recall_any@5 | recall_any@10 |
|---|---|---|
| **Graph-MIND** (479 questions) | **98.5%** | **99.2%** |
| agentmemory hybrid (500) | 95.2% | 98.6% |

## Reading other published numbers

They measure different things, so they do not compare directly:

- **MemPalace's 96.6% and agentmemory's 95.2%** are retrieval recall (R@5): is the right session
  among the five returned? This page measures whether the final answer is correct.
- **Mem0's 94.4%** is its managed cloud platform, with proprietary components; the open-source
  package tested here is a different system.
- **Mastra (94.87%), Emergence (86%), Supermemory (85.2%) and Zep (71.2%)** report their own
  setups and answer models; they were not reproduced here.

As far as we know, everything above 85% on that list runs a model over your conversations when it
saves them. Graph-MIND does not.

## Reproducing

1. Download `longmemeval_s_cleaned.json` from [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
   into `research/external/longmemeval/`.
2. Set `OPENAI_API_KEY` for the answer model and the judge.
3. `pip install -e .`, then from `research/` run the commands in
   [REPORT.md §10](../research/REPORT.md#10-reproducing). A full 500-question run costs about US$3.

## Known limits

- **For the first few seconds after an AI app starts, answers use word search only** while the
  embedding model loads (6-30 s on a slow, synced disk). The first answer never waits for it.
- **Daily use on Windows only so far.** macOS and Linux pass the tests in CI but have not seen real
  use.
- **Capture follows each app's transcript format,** which is not a public interface; an app update
  can stop capture until Graph-MIND is updated.
- **Multi-session questions are the weakest type at 80%**: counting or combining facts across many
  conversations.

The full list is in [REPORT.md §9](../research/REPORT.md#9-known-limits).
