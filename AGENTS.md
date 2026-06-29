# Project Objectives
Implement an FPGA back-end timing closure agent that optimizes `.dcp` files, with the following requirements:  
- Ensure that the logical equivalence of the optimized design remains unchanged.  
- Develop a comprehensive toolchain and strategy library that covers various optimization methods for different types of designs.  
- Employ elegant context engineering and loop engineering to provide the LLM with accurate and sufficient information for making optimization decisions.

# Reference
- `Vivado_RapidWright_Usage_Reference.md`
- `FPL26_Claude_Code_Reference.md`
- `README.md`
- `PROJECT_TREE_AND_DATA_FLOW.md`
- `architecture.md`

# ALWAYS:
- ALWAYS update `README.md` 、`PROJECT_TREE_AND_DATA_FLOW.md` & `architecture.md` after task accomplished(if necessary).
- ALWAYS use English to write code comment, but use Chinese to write plans.

# Guidelines:
## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
