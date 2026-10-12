# Lab 8 Viva Q&A (10 Questions + Answers)

1. **Q:** What is the difference between the parallel and sequential swarm topologies?
   **A:** They differ only in the swarm entry node after `workers`. Parallel uses `swarm_dispatch --[cond Send(swarm_worker)]--> swarm_worker --> swarm_merge`. Sequential uses `swarm_dispatch --> swarm_loop` (one node runs all 39 candidates). Both feed into `validate` (Lab 7 unchanged). The compiled graphs are compared by edge sets, not by constants.

2. **Q:** Why is a `Send` list only returned from a conditional edge path function, not a node?
   **A:** LangGraph 1.2.12 requires the `Send` list as the return value of the conditional-edge path function; returning it from a node raises `InvalidUpdateError`. Also the conditional edge must be added with the explicit path list (`["swarm_worker"]`) so the fan-out edge appears in `get_graph()`.

3. **Q:** Why pin `SWARM_MAX_CONCURRENCY = 8` instead of leaving the pool unbounded?
   **A:** Measured: unbounded gave unstable wall-clock (1.27–1.58x in plain process, 0.71x in Jupyter kernel) due to GIL-serialised LangGraph bookkeeping with many threads. Width 8 won in both (1.38x/1.68x). The bound is also structural: `2 <= max_overlap <= SWARM_MAX_CONCURRENCY` proves concurrency and boundedness without relying on a single timing measurement.

4. **Q:** Why does the merge recompute `alignment_to_plan` instead of trusting `self_reported_alignment`?
   **A:** The merge selects by plan alignment; recomputing from candidate content + locked plan means selection is verified, not self-reported. The candidate’s number is a claim recorded in the log for audit.

5. **Q:** Explain “the nicest loses”. When does it fire and when is it just a tie-break?
   **A:** `generous` inflates drafts by 10%. On most submissions it stays within rubric and ties strict at alignment 1.0 → loses on declared strategy order (deterministic tie-break). On cases where inflation exceeds rubric (e.g. near max), it violates `within_rubric_maximum` and scores lower (0.6) → screen fires. The log shows both cases.

6. **Q:** Why is `swarm_candidates` an `append` channel (not `LastValue`)?
   **A:** 39 workers write concurrently. With `LastValue` they would clobber each other (merge sees ~1). `Annotated[list[dict], append]` accumulates all candidates; `TypedDict` inheritance means reducers on inherited keys persist, and `get_type_hints(..., include_extras=True)` is needed to see `Annotated` metadata.

7. **Q:** What is the only GIL-releasing work here, and why does that matter?
   **A:** Per-candidate evidence write: `open → write → flush → os.fsync`. Pure-Python CPU fan-out gave ~0.99x (no speedup). Overlapping durable I/O releases GIL, producing the parallel win. This matches R11/R14 (log every candidate/evidence record).

8. **Q:** How does resume work and why is it cross-process safe?
   **A:** `resume_swarm(run_id, decision, root=...)` reopens the checkpointer at `out/lab08/<run_id>/checkpoint.sqlite`, builds the graph with the same config, calls `graph.invoke(Command(resume=decision), cfg, recursion_limit=...)` and absorbs state. It uses the stored thread/run_id and does not re-run pre-gate nodes (check 21). The checkpoint id format is ordinal-based (`write_seq:012d-digest12`) to avoid string-compare order issues.

9. **Q:** What triggers `MAX_ATTEMPTS` escalation and what is included?
   **A:** When the swarm cannot converge (injected defect) after 3 attempts, the bound escalates to `course_faculty` with a diagnosis and the failing draft. The exhaustion scenario logs every attempt’s candidates (117 total) and names the failing checks (`draft_grade_within_rubric_maximum`, `similarity_score_attached`).

10. **Q:** If this were real LLM calls (not scripted), would the bound still matter? Why?
   **A:** Yes. Network I/O is also GIL-releasing and highly parallelisable, but uncoordinated fan-out still causes contention, queueing, rate limits and memory pressure. A measured/constrained `max_concurrency` (and per-attempt bounds) is correct engineering; the structural `max_overlap <= width` remains meaningful and the design (separate dispatch/merge, append reducer, durable logging) scales to I/O-bound real work.
