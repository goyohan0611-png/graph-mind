# Graph-MIND — a local memory engine, measured honestly

**One line.** A memory store that keeps your conversations verbatim on your own machine and hands
whatever model you use the few thousand tokens that actually answer the question.

**Headline.** On 40 LongMemEval_S questions that nobody had tuned on — pre-registered with code
hashes before any run, same reader, same answer prompt, official judge — the recall path this
project ships answered **34 of 40 (85.0%) with no model call at write time**. Mem0 answered 21 and
MemPalace 23 in the same harness; both gaps are significant (exact McNemar p = 0.002 and 0.003).

| same 40 questions (§5a) | accuracy | 95% CI | write-time model tokens / question | read tokens |
|---|---|---|---|---|
| Graph-MIND, extraction pipeline | **36/40 (90.0%)** | 77–96% | ~76k | 4.9k |
| **Graph-MIND, shipped recall path** | **34/40 (85.0%)** | 71–93% | **0** | 2.8k |
| MemPalace 3.10.0 | 23/40 (57.5%) | 42–71% | 0 | ~10k characters |
| Mem0 2.2.1 (gpt-4o-mini) | 21/40 (52.5%) | 37–67% | ~640k | 1.2k |

Over all 500 LongMemEval_S questions the shipped path scores **444/500 (88.8%, CI 85.7–91.3)**, and
**330/380 (86.8%, CI 83.1–89.9)** on the 380 it was never tuned on, at a median 3.4k tokens per
question and nothing at write time (§5c). That is with the 30-item packet adopted on 2026-10-08 by a
pre-registered rule; the 40-question table above was measured with the earlier 20-item packet.

Vendors' own LongMemEval reports, for orientation only — different readers, subsets and harnesses,
none replicated here: Mastra Observational Memory 94.87%, Emergence 86%, Supermemory 85.2%, Zep
71.2%. Every system near the top of that list runs a model over the conversation at write time; the
shipped path here does not.

What *is* comparable is the method: every number below was fixed before the data was seen, and the
failures are in the report.

---

## 1. What the engine does, and what it does not

**Does:** keeps the source text verbatim; extracts typed events with provenance; indexes locally
(on-device embeddings, no text leaves the machine); selects the evidence for a question; computes
counts and sums itself; keeps a per-entity history.

**Does not:** understand language, reason, or write the answer. That is the model the user already
runs. The engine also does not summarise the source away, and does not guess at write time what will
be asked later.

Per question it delivers ~4.7k tokens drawn from a ~125k-token haystack, so the cost stays flat as
the memory grows — the property that matters once a memory is years rather than weeks old. That
comparison is against putting the whole history in the prompt. Against a pure retrieval system that
runs no model at write time it is the other way round: see §5.

## 2. Results

Four evaluations, each on questions never used before, each with its question list, configuration and
code hashes frozen in a `preregistration.json` *before* the run, each graded by the official
LongMemEval judge protocol (`gpt-4o-2024-08-06`).

| # | date | n | accuracy | what changed since the previous one |
|---|---|---|---|---|
| 1 | 09-18 | 120 | 65.8% | first honest measurement; exposed that pilot tuning had not generalised |
| 2 | 09-21 | 120 | 77.5% | evidence assembly: event-anchored verbatim source turns |
| 3 | 09-22 | 100 | 75.0% | date resolver, engine arithmetic (no measurable gain) |
| 4 | 09-28 | 72 | **83.3%** | retrieval over user turns with k=16, multi-view entity queries |

Final run by question type:

| type | score |
|---|---|
| knowledge-update | 7/7 |
| single-session-assistant | 11/11 |
| temporal-reasoning | 20/24 (83%) |
| single-session-user | 5/6 |
| multi-session | 15/19 (79%) |
| single-session-preference | 2/5 |

95% CI for 83.3% at n=72 is 74.7–91.9%. The final set was deliberately the hardest mix drawn so far:
temporal + multi-session were 60% of it. LongMemEval_S is now fully spent — 500 questions, all used.

Cost, measured on the same runs: 4.7k input tokens and 2.9s median per question (p95 8.7s).

## 3. What was falsified

Two claims this project had made were tested and did not survive.

**"Accuracy does not depend on the reader model."** Same retrieval, same events, same evidence
packet, only the answering model changed:

| reader | accuracy (dev set, 120 q) |
|---|---|
| gpt-4o-mini | 73.3% |
| gpt-4o | 78.3% |
| gpt-5 | 82.5% |
| gpt-5-mini | 85.8% |

Spread 12.5 points — wider than Mastra's 10.6. Every number must name its reader.

**"A better model makes the system better."** Also false: `gpt-5` scored 3.3 points *below*
`gpt-5-mini`. It abstained 10 times against 6, being more cautious with the same thin evidence. The
bottleneck is the packet the engine hands over, not the model reading it.

## 4. What did not work

Recorded because a list of dead ends is the part of this work that is expensive to reproduce.

| change | result |
|---|---|
| Local answer-grounding gate (replacing the LLM sufficiency gate) | 85.0% → 40.8%: blocked 51 correct answers; computed answers quote no evidence |
| LLM sufficiency gate | cost 3 correct answers on held-out data; removed |
| Engine-built candidate list for the reader | no movement on either dev set, +340 tokens |
| Write-time `search_aliases` (other wordings of a fact) | evidence coverage 92.2% → 93.9%, inside noise; dropped on principle too — it is write-time guessing |
| Structured time offsets replacing date regexes | dated events 16.2% → 11.1%; models fill the numeric offset far less often than they leave a phrase |
| Chronological ordering of the event list | 85.8% → 83.3%: relevance order matters more than date order |
| Entity-based gathering inside the benchmark | evidence coverage 90.4% → 28.3% (see §6) |
| Bigger local embedding models (e5-base, bge-m3) | recall@5 80.4% → 81.2% / 78.0%: no gain |
| Cross-encoder reranking | abandoned: ~0.5–1s per candidate pair on CPU, unusable at query time |
| Reasoning effort `minimal` for extraction (−47% of the write bill) | evidence coverage 91.2% → 85.3%: 27% fewer events extracted, and 2 of 34 answer turns stopped reaching the reader |
| Batching several sessions per extraction call | the fixed prompt prefix is 537 tokens and input is 20% of the bill, so the ceiling is 3%; not built |
| Prompt caching for extraction | `cached_tokens` 0 on every session: the prefix is under the 1,024-token minimum |
| Loosening the extraction schema so empty fields could be omitted | `gpt-5-mini` emitted every key anyway (present on 100% of events with `strict` off), so the 185 tokens of forced nulls were not recovered; schema adherence would have been given up for nothing |
| Shipped path: ranking turns *said* near a resolved date ("two weeks ago") first | 11 dev2 questions: evidence up in 2, down in 2 (one 3 → 0) — people mention an event days or weeks before or after it happens. Kept only for "what did we discuss last week", where the said-date is the question |
| Shipped path: indexing the dates a turn *talks about* (rule-based, no model) | answer sessions covered 15 → 15 on the same 11; removed |
| Shipped path: one piece per conversation first, for counting questions | answer sessions covered 111 → 111 on 51 questions, answer-bearing pieces 530 → 344; removed |

## 5. Head to head with MemPalace, in this harness

MemPalace 3.10.0, installed from PyPI and left at its documented defaults, was given the same
haystacks; its retrieval output then went through **our** answer prompt, **our** reader and the same
official judge. Only retrieval differs.

| on the same 20 questions | accuracy | write-time LLM | read tokens |
|---|---|---|---|
| Graph-MIND retrieval | **17/20 (85%)** | 4.8k per session | 4.7k per question |
| MemPalace retrieval | 10/20 (50%) | **none** | 3.0k per question |

Read this with its caveats, which are as much of the result as the number:

- **n = 20.** MemPalace's 95% interval is 28–72%. The ordering is clear; the size of the gap is not.
- **The questions favour us.** They come from the set this project was developed against;
  MemPalace saw them for the first time.
- **The cost comparison favours them.** MemPalace spends no model tokens to store anything, by
  design. Graph-MIND spends ~4.8k per session extracting typed events, which on this benchmark —
  where every question carries its own 16-session haystack — works out to ~29x more total tokens.
  In real use that write cost is paid once and shared by every later question, but it is real, and
  the earlier claim that this system is simply "cheaper" was wrong: it is cheaper than stuffing the
  context window, not cheaper than a retrieval-only store.

So the honest summary of this comparison is: **on these questions we answer 35 points more of them,
and we pay for it at write time.**

### Mem0, the rival in the same class

MemPalace extracts with English keyword regexes, so beating it says nothing about whose *extraction*
is better. Mem0 does: like Graph-MIND it runs an LLM over each session at write time. Mem0 2.2.1 ran
in the same harness on the same 20 questions, with its full retrieval stack (BM25 hybrid search and
spaCy, which a plain `pip install mem0ai` leaves out) and its own prompts untouched.

| same 20 questions | accuracy | 95% CI |
|---|---|---|
| Graph-MIND | **17/20 (85%)** | 64–95% |
| Mem0 | 14/20 (70%) | 48–85% |
| MemPalace | 10/20 (50%) | 30–70% |

**The Graph-MIND–Mem0 gap is not significant.** Five questions only we answered, two only Mem0 did;
exact McNemar p = 0.45. The ordering is suggestive and that is all.

Where the gap comes from is clear, though. Mem0 scored 0/3 on questions asking what the *assistant*
said, and its retrieved memories show why: it records the user's request and drops the answer. Asked
how many subjects a study had (38), how many mummies a one-shot contained (4), or which resource was
recommended (Mindful.org), it returned "User requested specific examples of studies…", "User requested
a D&D one-shot…", "User requested recommendations…" — no assistant-subject memory in any of the
three top-15s. Graph-MIND's extraction prompt keeps exactly that ("a NAMED recommendation, a specific
title, place, product, number or step it gave").

Write cost, **measured** by intercepting every OpenAI call Mem0 makes (it exposes no accounting):

| per session | Mem0 (gpt-4o-mini) | Graph-MIND (gpt-5-mini) |
|---|---|---|
| LLM calls | 1 | 1 |
| input / output tokens | 10,556 / 329 | 3,419 / 1,229 |
| embedding tokens | 2,303 | 0 |
| total tokens | 13,188 | 4,648 |
| cost | $0.00183 | $0.00331 |

Mem0 uses 2.8× the tokens and Graph-MIND spends 1.8× the money — output costs eight times input
and the two designs are mirror images. At one model's prices it flips (Mem0 $0.00183, Graph-MIND
$0.00125), so the higher bill is the model choice, not the design. Three earlier claims of mine about
this comparison were wrong and were corrected by measuring: Mem0 makes one LLM call per session, not
two; its write bill is lower, not higher; and a 6-hour vs 9-minute wall clock compared my harness
(eight parallel workers against a serial loop), not the systems. What does hold is structural:
Graph-MIND extracts each session independently, so writing parallelises freely, while Mem0's `add()`
consults the store and cannot be parallelised within one.

Two fairness notes. Mem0 2.2.1 does not run at its defaults on OpenAI — its default model
`gpt-5-mini` is missing from its own reasoning-model list, so it sends `temperature=0.1` and gets a
400 — so its documented parameters were kept and a model that accepts them (`gpt-4o-mini`) was
named. And it received each question's full haystack, more than Graph-MIND extracts here.

**These are still the questions Graph-MIND was tuned on.** The same comparison on 40 questions
nobody was tuned on is §5a.

Two cost changes were then measured on those same 20 questions.

**Extraction was trimmed** — no write-time alias generation, no structured time offsets, and
assistant-side events limited to things a question could ask it to repeat (41% → 24% of events).
Output tokens per session fell 23% (2,168 → 1,672) and accuracy on those 20 questions moved 17 → 16,
inside the noise described in §6.

**The evidence packet was reshaped** from 15 spans of 1,000 characters to 20 of 500. Span count is
what decides whether an answer-bearing turn arrives at all (8 → 88.7%, 15 → 92.2%, 20 → 92.6%
coverage); span length decides only whether the truncation keeps the answering sentence, and the gold
answers sit at most 481 characters in, so 500 loses none while 300 loses 4.9% of them. Accuracy at
20×300 was 17/20, unchanged. This was **not** a token win: reader input tokens measured 4,730 → 4,870
(+3%), because 5 more spans cost more than the shorter cap saves. It was bought as coverage, not cost.

**Six schema fields were deleted.** Storage is billed almost entirely on output — per session
3.1k input at $0.25/M is 20% of the cost, 1.6k output at $2/M is 80% — so the input-side ideas
(batching several sessions into one call, prompt caching) were dropped after measurement: the fixed
prompt prefix is 537 tokens, so batching could reach 3% of the bill, and `cached_tokens` came back 0
on every session because that prefix sits under the 1,024-token minimum caching needs. The output
side had real waste. A strict JSON schema makes every property required, so a field nothing reads is
still emitted as an explicit null on every event: `confidence`, `action`, `value_type`, `numeric_min`,
`numeric_max` and `boolean_value` had no reader anywhere in the retrieval or answer path — the
reader's event list never included them, and the ingestion gate used them only for an EVIDENCE_ONLY
status the executor discards. Removing them cut 246 output tokens per session (15% of output, 12% of
the bill). `schema_trim_equivalence.py` proves the change is inert: 8,208 field values were stripped
from 2,699 events of a finished run and the recomputed retrieval ranking, reader event list, evidence
packet and acceptance decision are byte-identical for all 20 questions.

The first version of the packet change shipped at 20×300 on an ablation that reported identical coverage for
300 and 1,000 characters. That column was flat by construction — the hit test searched for a marker
taken from characters 40–120 of the turn, which no cap tried could cut — so the length axis had never
been measured. It is now its own check (`span_cap_check.py`), and the episode belongs in §6: an
offline metric is only as trustworthy as the thing it is actually sensitive to.

## 5a. The pre-registered clean comparison

`runs/graph-v6.0-rival-clean/preregistration.json` froze 40 questions from the one pool no
development script had read (v3.0, measured once at 65.8% with a pipeline five versions old),
stratified by type and ordered by the sha256 of each id, together with the sha256 of the 13 files
that make up every arm. Six amendments were logged before any result existed — among them adding the
shipped recall path as a fourth arm, and making its loader skip empty turns as the capture service
does — and each asserted the question list unchanged. After the first result the manifest locked.

| type | n | extraction pipeline | shipped path | Mem0 | MemPalace |
|---|---|---|---|---|---|
| knowledge-update | 7 | 6 | **7** | 2 | 6 |
| temporal-reasoning | 11 | **10** | 9 | 4 | 8 |
| multi-session | 9 | **7** | 6 | 6 | 2 |
| single-session-user | 6 | 6 | 6 | 6 | 3 |
| single-session-assistant | 4 | 4 | 4 | 1 | 2 |
| single-session-preference | 3 | 3 | 2 | 2 | 2 |
| **all** | 40 | **36** | **34** | 21 | 23 |

Paired, question by question (exact McNemar): shipped path vs Mem0 15 against 2, p = 0.002; vs
MemPalace 12 against 1, p = 0.003; extraction pipeline vs Mem0 16 against 1, p = 0.0003. The two
Graph-MIND paths split 3 against 5, p = 0.73: equivalent. Mem0 and MemPalace are indistinguishable
from each other (p = 0.84).

Where the rivals lose is specific. Mem0 rewrites each session into short memories, and the rewrite
drops what later questions need: what changed (2/7 on knowledge updates), when (4/11 on temporal
questions), what the assistant said (1/4). MemPalace keeps text but files and ranks it by keyword
rooms, and loses where a question needs several sessions at once (2/9).

What the comparison does **not** show:

- **n = 40.** Each arm's interval is ±13–15 points. The gaps to both rivals are larger than that;
  the gap between the two Graph-MIND paths is not.
- **Mem0 ran on gpt-4o-mini**, because its defaults fail on OpenAI (§5). A stronger extraction model
  might help it.
- **Our harness, our reader.** Every arm's retrieval output went through the same answer prompt and
  `gpt-5-mini`; neither rival was used through its own answering stack.
- **Input differed.** Mem0, MemPalace and the shipped path received each question's full haystack
  (1,893 sessions over the 40); the extraction pipeline extracts only the sessions it retrieves (673),
  which is why its per-session cost is higher and its per-question cost is not.

Write cost, measured: the extraction pipeline spent 3.05M tokens ($2.25) over the 40 questions, Mem0
25.6M ($3.52, intercepted at the API), MemPalace and the shipped path none. Mem0's `add()` consults
its own store, ran ~18 minutes per question and cannot be parallelised within one.

**Pipeline hygiene, found during this run.** The Graph-MIND arm's first extraction pass ran on a
revoked API key: all 640 calls returned 401, and the resume logic counted any written row as done, so
17 answers were built on no extraction at all. They were moved to `invalid-401/` and redone with a
valid key before anything was judged. The official-judge loop had the same defect (one Mem0 verdict
timed out and was regraded), and so did the rival answer loop (one MemPalace answer came back empty
and was asked again). All three were fixed at the root once the comparison was over and the frozen
files could change: a resumed run now retries every row whose latest status is not `ok`
(`test_resume_retries.py`).

## 5b. The shipped recall path

Until this round every published number came from the extraction pipeline, which shares no code with
the MCP server people run. Measuring the server's own path (`product_recall_eval.py`,
`product_answer_eval.py`) on the tuned dev2 set showed it delivering only **48.5%** of answer-bearing
turns. Each change below was measured offline before any answer was paid for:

| change | answer-bearing turns reaching the packet |
|---|---|
| shipped path as it was (word search, 12 items, keyword router) | 48.5% |
| router off: the calling model has already decided memory is needed | 66.7% |
| meaning search over user-turn 500-character pieces, fused with word search | 90.4% |
| 20 items / 10,000 characters | **93.9%** |
| 30 items / 15,000 characters (adopted after §5c's full-500 test) | — |

End to end on dev2 that reads **88.3%** against the extraction pipeline's 85.8% (McNemar p = 0.68,
equivalent), with no write-time model call. Its one weak type was questions about what the assistant
said (8/12): those answers run 1,000–4,000 characters and a 500-character piece rarely held the number
asked for. When the caller marks a question as being about the assistant, its own past answers are
searched by meaning too and delivered whole up to 3,000 characters: 10/12 on dev2, 4/4 on the clean
set.

## 5c. All 500 questions, shipped path

Every LongMemEval_S question through the shipped path, same reader and judge. 120 of them are the
dev2 set this path was tuned on, so they are reported apart; the other 380 include the 40 clean ones
of §5a. Two packet sizes, the same 500 questions in the same order:

| type | tuned 120 | untuned 380 | all 500 | all 500, 20-item packet |
|---|---|---|---|---|
| single-session-user | 17/17 | 46/47 | 98.4% | 98.4% |
| single-session-assistant | 11/12 | 42/44 | 94.6% | 94.6% |
| knowledge-update | 19/19 | 49/53 | 94.4% | 93.1% |
| abstention | — | 28/30 | 93.3% | 90.0% |
| temporal-reasoning | 31/34 | 81/93 | 88.2% | 84.3% |
| multi-session | 30/32 | 67/89 | **80.2%** | 73.6% |
| single-session-preference | 6/6 | 17/24 | 76.7% | 80.0% |
| **all** | **114/120 (95.0%)** | **330/380 (86.8%)** | **444/500 (88.8%)** | 430/500 (86.0%) |

**Why 30 items.** With 20 items / 10,000 characters (`runs/graph-v6.9-full500-warm`) multi-session
was the weak type, and the packet held every answer session in only 79 of the 89 untuned ones. A
probe on halves of the set showed 30 items raising that coverage, so the larger packet was tested on
all 500 under a rule written before the run (`runs/graph-v7.0-full500-30/PREREGISTRATION.md`): adopt
only if the overall paired gain is significant. It gained 23 answers and lost 9 (exact McNemar
p = 0.020; untuned alone +15/−7, p = 0.13). Multi-session gained most (+11/−2, p = 0.022), and
coverage rose to 84 of 89. Temporal reasoning gained +7/−2. Preference went the other way, 21 → 17
of the untuned 24 (p = 0.13). Giving advice requests the smaller packet does not recover it: on the
28 questions an advice rule selects, the 20- and 30-item packets both score 22, so the drop is run to
run noise, and no size switch was added. The cost is a median packet of 3.4k tokens instead of 2.3k. One question
(gpt4_d6585ce8) returned no parsable answer after three tries and counts as wrong. `brain_context`
now defaults to 30 items / 15,000 characters.

**A measurement error, caught and corrected.** The first pass over the 500 scored 79.2%. The
shipped server never makes a question wait for embedding: when more than 64 pieces of the store are
unembedded it answers from word search and fills the vectors in the background. Most of the 500
haystacks were new to the cache, so most of those packets were built on word search alone — a cold
start, not the steady state the capture service keeps a real store in. The packet stage now embeds
each haystack before searching; 337 of 500 packets changed, and the re-run gained 38 answers and
lost 4. The cold-start behaviour itself is real and is listed in §9.

## 5d. Korean, on a real store

LongMemEval is English. A local script (kept out of this repository, since its questions are about
the author's own history) asks 16 questions about this project's own history,
in Korean, the way its user asks them. The store is the user's live one: about 4,300 captured Claude
Code and Codex turns. Answers were fixed in advance as keywords, and the script prints only hit and
rank, never memory text. Nothing leaves the PC. Eight questions are about what the user said and
eight about what the assistant said (`about_assistant`).

**15 of 16 packets hold the answer.** The miss: "MemPalace는 몇 점이었어?" Dozens of turns mention
MemPalace, and the one with its score is not among the 30 delivered. One answer arrived at rank 30,
the last slot: the 20-item packet would have missed it. Median packet 3.6k tokens.

## 6. The methodological finding

Two runs of one configuration — verified byte-identical request bodies for all 120 questions —
scored 103 and 100 of 120. A third scored 98. **Nine of 120 answers move between runs of the same
setup.**

Consequences, applied retroactively to this report: differences under ~5 points on a 120-question set
are not interpretable, so only changes with large repeated margins are claimed, namely

- retrieval indexed over **user turns** rather than serialized sessions: sessions whose evidence all
  lands in the top-8 went 105/120 → 115/120;
- **k=8 → k=16**: on held-out data, k=8 left 10% of questions without their evidence and 9 of those
  10 were answered wrong; recall@16 = 98%; dev2 79.2% → 84.2% (+8/−2);
- **event-anchored verbatim passages**: share of answer-bearing turns actually reaching the reader
  40% → 95% at equal tokens.

Everything else in this project's history sits inside the noise band and is reported as such. A/B
decisions are now made on deterministic offline metrics (evidence coverage, recall@k) rather than on
a single accuracy run. The _M recall in §7 read 100% at n=8 and settled at 90% by
n=20; a small offline sample misleads exactly like a small accuracy run does.

## 7. Does it hold as the memory grows?

This is the claim the project is sold on and it had never been tested: every number above comes from
LongMemEval_S, where a question's haystack is ~47 sessions, so keeping the top 16 throws away only two
thirds of the store. _M asks the **same 500 questions** over ~476 sessions each. It cannot produce a
fresh accuracy number — the questions are spent — but retrieval there is decidable for free: the index
is local embeddings and the answer sessions are labelled. 20 questions, measured on both.

| | _S | _M |
|---|---|---|
| sessions per question | 46.7 | 475.6 |
| recall@8 | 100% | 85% |
| recall@16 | 100% | 90% |
| recall@21 | 100% | **100%** |
| worst answer-session rank | 5 | 20 |
| median worst rank | 1 | 1 |

**Ten times the haystack moved the worst rank from 5 to 20.** Eighteen of the 20 questions rank their
evidence in the same place as before; the cost of a bigger memory falls on a small tail, not on the
median. Two questions fell outside k=16 — one multi-session, one preference, the type that is weakest
on _S too.

So the flat-cost claim survives, but the fixed `k=16` does not: it leaves 10% of questions on a
476-session store without their evidence. `k` now scales with the store. Two points — k≥6 at 47
sessions, k≥21 at 476 — and `sqrt` fits both almost exactly (0.88 and 0.96 of √n), so `k = max(16,
⌈1.5·√n⌉)`, which opens 33 sessions of 476 and 336 of 50,000. A constant *share* of the store fits the
same two points and was rejected: 7% of a 50,000-session memory is 3,500 sessions to open, which would
destroy the property this is meant to defend. Two points cannot tell `sqrt` from a logarithm, the 1.5×
margin is deliberate, and **nothing here is validated beyond 476 sessions.**

`longmemeval_m_recall.py --report` asserts that the shipped rule still covers both measurements, and
records each answer session's *rank* rather than a hit at one k, so any k is a slice of the same
measurement and never needs re-embedding.

## 8. The entity index, measured at last

`brain_timeline` (one entity's history, oldest first, superseded entries marked) has been in the
product unvalidated. The earlier attempt measured the wrong thing: it asked whether gathering evidence
by entity beats gathering it by similarity on LongMemEval, and lost 90.4% → 28.3%. That says little
about the feature, because LongMemEval questions never name an entity — so the plan to re-run it on
_M was dropped: the cause is the benchmark's shape, not the store's size, and it would have cost real
money to reproduce a result already explained.

What the feature claims is narrower and free to check. A store was built from 4,195 real extracted
events at their real dates, and the index measured against the ground truth of which memories list
which entity (`entity_index_check.py`).

| | result |
|---|---|
| recall of an entity's memories | **100%** |
| oldest-first ordering | **correct** |
| exact-name precision | 0.81 mean, 0.13 worst |
| **entities that occur exactly once** | **97.7%** (2,026 of 2,074) |
| entities occurring 3+ times | **16** |
| median words per entity name | **7** |

The mechanism is right and the vocabulary is not. An index whose entity names occur once each has no
history to return, however correctly it returns it. The extraction prompt never asks for entity names
— it asks for the subject and object of a relation — so `object_text` arrives as a phrase: the
busiest "entities" here are `user` (1,583) and `assistant` (439), then a handful of real ones, and one
name is 166 words long.

This is the contract the product already states: the entity names come from the model that writes the
memory, because naming is a language judgement. **Nothing about that contract is validated by any
benchmark**, and no LongMemEval variant can validate it, because no LongMemEval question asks for an
entity's history. It needs real use, or a dataset of that shape.

One design note from the measurement: `timeline()` matches names by substring as well as exactly. For
a person that mostly helps — "lunch with Rachel" and "5K run with Rachel" really are Rachel's history
— but for a common noun it conflates unrelated things (15 memories for "budget", each about a
different budget), and the rule is one-directional: asking for "lunch with Rachel" does not return
"Rachel". Low exact-name precision is therefore not automatically a defect, and is reported as what it
is rather than as a score.

## 9. Known limits

- **One language-specific component remains**: English regexes that resolve "last weekend" style
  phrases when the extraction model leaves a date empty. Korean falls back to no date.
- **Validated only to 476 sessions.** The `sqrt` rule for `k` is fitted to two store sizes.
- **The entity index works but has never been fed real entity names** (§8). Its correctness is
  measured; its usefulness depends on the calling model naming entities, which no benchmark tests.
- **No abstention guarantee.** The engine-level gate is off; whether to answer is the reader's call.
- **LongMemEval_S is exhausted** for pre-registered claims; §5c reports all 500 with the tuned 120
  split out.
- **Multi-session is still the weakest type off the tuned set** (67/89 with the 30-item packet). Most
  remaining misses hold every answer session (84 of 89 do) and fail in the reader's counting.
- **Cold start, reduced.** The capture service now embeds every turn it stores (the user's first,
  then the assistant's) and the memory servers take those vectors from the shared cache before each
  search (`test_cold_start.py`). Each server also loads the embedding model at start-up: a fresh
  server's first question dropped from 30-50 s to 2 s. A fresh install with a large imported history
  still waits for the service's first passes, about ten minutes for this user's 4,300 turns.
- **Korean is checked, not benchmarked.** §5d is 16 questions on one user's store.

## 10. Reproducing

```bash
python untouched_eval_final.py     # the 83.3% run (pre-registration is written before any call)
python reader_swap.py gpt-4o gpt-5 # the reader ladder
python retrieval_ablation_v32.py   # recall@k, offline, no API
python entity_packet_ablation.py   # entity vs similarity gathering, offline
python packet_shape_ablation.py    # how many evidence spans, offline
python span_cap_check.py           # how long each span may be cut, offline
python longmemeval_m_fetch.py      # LongMemEval_M, 2.74 GB, streamed and split per question
python longmemeval_m_recall.py     # does retrieval hold at 476 sessions, offline
python entity_index_check.py       # the entity index against real extraction output, offline
python schema_trim_equivalence.py  # the deleted schema fields change nothing, offline
python reasoning_effort_probe.py   # extraction cost per reasoning effort
python effort_coverage_check.py    # and what the cheap effort costs in coverage
python strict_mode_probe.py        # strict vs optional schema fields
.venv-rivals/Scripts/python.exe rival_mempalace.py mine --limit 20      # MemPalace, its defaults
.venv-rivals/Scripts/python.exe rival_mempalace.py retrieve --limit 20
python rival_mempalace.py answer --limit 20                            # our reader + official judge
python rival_mem0.py add|retrieve|answer   # Mem0, its own venv; see the file for sharding
python rival_clean_prereg.py --show        # the frozen 40-question clean comparison
python clean_graph_mind.py                  # its extraction-pipeline arm (refuses on hash drift)
python product_recall_eval.py               # shipped path: evidence reaching the packet, offline
python product_answer_eval.py spans|answer  # shipped path end to end; --ids/--cache for the clean set
python temporal_resolver.py        # unit self-checks (also: vector_cache, entity_timeline,
                                   # embedding_warmup, brain_log)
```

Each run writes its questions, answers, judge verdicts and a frozen manifest under `runs/`, so any
number above can be recomputed or disputed from the artefacts.

## 11. Using it

An MCP server; the memory is SQLite files plus a local vector cache in a folder the user chooses.

- `brain_remember` — store a memory; it is indexed and embedded on the way in
- `brain_recall` / `brain_context` / `brain_associate` — retrieve (~2.4k tokens of verbatim evidence)
- `brain_timeline` — one entity's history, oldest first, superseded entries marked
- `brain_index` — bring an imported backlog up to date

Set `GRAPH_MIND_FOLDER` to a synced folder (Dropbox, OneDrive, Syncthing) or a USB stick and each
device writes its own file inside it while reads merge across all of them: several machines, no
server, no account, no conflicts. Or set it to `local` — Postgres on this PC, started on first use
(pgserver) — and every client sees a new memory the moment it is written, with no sync delay; another
PC joins it with a connection code. Either way each PC keeps its own SQLite index, and when the
shared brain is unreachable recall answers from that index. There is no cloud component, and nothing
is sent anywhere except the calls the user's own model makes.

Joining takes two sentences and no typing of addresses:

- on the PC that holds the brain, "let my other PCs in" → `brain_folder(path="share")`. Postgres
  starts listening beyond localhost on a fixed port (54329). A Windows firewall rule admits only the
  local subnet and Tailscale (100.64.0.0/10); adding it needs one administrator approval. The reply
  is a `gm1.` code carrying this PC's network and Tailscale addresses, the port and a generated
  password.
- on the other PC, `python install.py --join gm1.…` (or paste the code to its AI). Each time it
  connects it uses the first address that answers. A laptop therefore reaches the same brain over
  the office network and over Tailscale from home.

Other PCs log in as `graphmind`, a role that is not a superuser, into the `graphmind` database only,
and only with the password. The administrator role and password-less access stay on 127.0.0.1
(`test_shared_brain.py`: wrong password refused, admin refused over the network). `install.py`
installs the packages and registers the server with Claude Code, Claude Desktop (including the
Store build) and Codex. It also adds the capture service to login and pre-downloads the embedding
model. Re-running it after moving the folder re-points everything (`test_install.py`).

Captured automatically every two minutes, secrets masked:

| client | captured |
|---|---|
| Codex — terminal, VS Code, ChatGPT desktop's work mode | every turn |
| Claude Code — terminal, VS Code, Claude desktop's Code tab | every turn |
| Claude desktop Cowork | every turn |
| Claude desktop chat | what the model saves with `brain_remember` |
| ChatGPT chat | not captured |

Claude, Codex and the capture service share one store from separate processes. SQLite runs in WAL
mode with a 30-second busy wait, and the vector cache is saved as a new file plus an atomic index
swap, so no reader can pair one process's index with another process's vectors
(`test_concurrent_use.py`, `python vector_cache.py`).
