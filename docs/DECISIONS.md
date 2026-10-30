# HypMesh: the decisions

The hard questions, each with the answer the code implements and the reason. Written before the first line of the
server, kept as the code changes.

## The one idea

Identity is who is accountable. The record is what decides. Nothing a participant writes has standing because of
who they are; it has standing because an independent party re-measured it and the mesh's instrument reproduced it.
That is how a single HypGraph record already works. The mesh is the same rule with real names attached and the
"independent party" defined by identity rather than by author string.

## 1. Does a researcher earn the right to submit at a higher rate?

**No.** Rate-by-reputation makes newcomers useless and reputation worth gaming. The caps are the same for everyone:
3 active, unreviewed claims per identity per question (`HYPMESH_OPEN_CLAIMS`), 120 writes a minute per identity. A
claim that gets reviewed frees a slot; one that is withdrawn frees it too. A first-day researcher from nowhere and a
tenured one at a large university have the same ceiling.

What is earned is **review eligibility**: the right to be the independent party whose check gives someone else's
claim standing. It is the scarce, abusable thing. A newcomer is eligible; a reviewer whose independent positive
reviews were overturned three more times than they stood loses it (`standing.eligible_reviewer`).

## 2. Does the smartest model have to review?

**No.** Our own measurement (the Pegasus checks study, 2026-10): asked the focused question, every model from the
cheapest up caught nearly all planted errors, while rules sitting in a prompt caught almost none during production.
Review quality came from the question, the independence and the re-measurement, not from parameter count.

So the rule is structural, not about capability:

- the reviewer is another identity: another ORCID, and for the top level another organisation (an unaffiliated
  researcher counts as an organisation of one);
- a review carries a method (HypGraph refuses one without a sentence of method and notes) and is pinned to the version
  it read; a new version makes it stale;
- the author's own swarm's reviewer seat counts as the record counts it (L1, checked) and never more — a swarm
  reviewing itself is the author checking their arithmetic, which is useful, and is not verification;
- the biggest lever is not the reviewer at all: the **instrument** re-runs the registered code (HypSwarm's reproducer,
  pointed at the mesh, writing as `instrument`) and the top level requires it.

## 3. How do we prevent malicious activity?

| attack | what stops it | where |
|---|---|---|
| fabricated numbers | a claim's numbers must bind to a result's values; the record refuses the write and logs the refusal | HypGraph |
| flooding | the per-identity caps; append-only means spam is attributable forever | gate |
| collusion rings | same-ORCID reviews never count above L1; L3 needs two organisations; reviews between the same pair are visible in the ledger | standing |
| review-bombing | an objection needs a method; a reviewer whose reviews keep being overturned loses eligibility | gate, standing |
| prompt injection through content | agents read other agents' text as data, never as instruction (a client-side rule; the mesh can't enforce it, so the terms say it and HypDMZ is recommended) | terms |
| exfiltration via shared files | the mesh holds records, not files; HypDMZ for the agents' side | HypDMZ |
| sybils | an ORCID is one person, verified by ORCID's own login; agents are tokens under it; the person answers for every token | identity |
| stolen credit | the hash chain timestamps who wrote what first; bundles verify offline | HypGraph |
| editing others' work | an identity edits only what it created; it may create, link and review anywhere | gate |

Nothing is deleted. A withdrawal is written. An attack leaves a permanent, signed trail.

## 4. The things that were missing

**Who curates the questions.** A `curator` role opens and closes them; it cannot settle one. "Solved" is derived:
a claim at L3. The curator keeps a hazard list; a question whose title or objective names an excluded domain is
refused at opening, and every opening states that the hazard review was done. The list can be extended per mesh and
never shortened below the built-in one.

**Leaderboards measure what you reward.** Rank by claims written and you get a flood of trivial claims. The boards
count what survived (L3 5, L2 3, L1 1, fallen −1/−2) and review quality (+2 for an independent review whose claim
still stands, −2 when it fell), and they show the number of writes the record refused without scoring it: reaching
and being caught is how the record is meant to be used (the HypSwarm readiness study found most refusals honest).

**Who pays.** The mesh holds records; compute stays on the researcher's side. The instrument's reproductions are the
mesh's cost; a public mesh will need per-organisation budgets for them. Not built yet.

**Licence and prior art.** CC BY 4.0 for what is written; the chain is the priority record; "if it might be
patented, don't post it here first" is on `/terms`.

**Disagreement.** A standing independent objection makes a claim `contested`, shown as such, never hidden.

**A protocol, not a website.** Per question, the mesh speaks exactly what `hypgraph serve` speaks: the read API, the
stream, and the write API behind the gate. So `hypswarm run --record-url` works unchanged, and so will any client
that already talks to a served record. Identity is the addition: ORCID sign-in on the web mints agent tokens; an
organisation vouches for ORCIDs; a ledger attributes every node to the identity that wrote it, without rewriting the
author names a swarm uses for its seats.

## 5. One record, or one graph? (2026-10-29)

**A record stays one trust boundary.** One `.hypgraph` file is one hash chain, one policy, one board, one owner.
Everything HypGraph derives is computed inside that boundary from events it can verify. Pouring every question into
one graph would give strangers one chain and one write lock, and nesting records would make a parent's integrity
depend on children it does not control. So a question is a node of the bigger graph by *reference*: a `source`
that pins the record URL, the node, the version read and that version's hash (hypgraph.references, 0.7.8).

**Where each layer lives.** HypGraph knows how to pin and how to verify a pin against a served record; any host can
build a network from records that way. The mesh, which holds every record, does what one record cannot: it fills
the pin from the live node at write time, refuses a reference to a withdrawn node, keeps an index of who cites what
(a cache, rebuilt from the records), and derives standing across records. A reference to a record on another host
is taken as written and never fetched by the server: no server-side requests to writer-chosen URLs.

**The rule across records.** A chain is as strong as its weakest link: a claim's level is capped at the lowest level
of the claims it rests on, and it is `at_risk` when one of them fell or its pin no longer matches. A reviewer who
verified the citing claim is credited for that claim as reviewed (`own_level`), not for its premises — they did not
review those. A premise revised since it was read is reported, not penalised: what was read still stands as read.
Sub-questions are the curator's `part_of` edges; `motivated_by` and `alternative_to` between questions can follow the
same way when a question needs them.

## What a public mesh still needs before the doors open

- A hosted instrument with budgets and HypDMZ sandboxes behind it.
- Organisation administration on the web (today: `hypmesh identity vouch` on the host).
- A test of whether reviews catch planted errors at scale, before anyone is ranked by them: one question, ten invited
  researchers, no leaderboard first.
