# AGENTS.md — BugLens AI engineering rules

Binding on every agent and contributor working in this repository.
When in doubt, stop and report BLOCKED rather than improvising.

1. Never fabricate model responses, test results, or PR URLs.
   Report only outputs actually observed from tools, tests, and APIs.
2. Missing mandatory evidence means BLOCKED.
   Do not proceed to patching, verification, or publishing without it.
3. Pin repository commits and freeze reproduction tests before patching.
   No patch work starts on a moving target.
4. Candidate code cannot alter protected tests or the trusted verifier.
   Patches touch approved source files only; test/verifier changes are forbidden.
5. Keep provider credentials server-side and out of execution sandboxes.
   Browser/Modal sandboxes receive no API keys or tokens.
6. Only approved source files may change.
   Anything outside the approved file list is out of scope for a patch.
7. Limit repair attempts to three, with time and cost budgets.
   After the third failed attempt, stop and report instead of retrying.
8. Publish only the tested change; never auto-merge.
   A PR contains exactly the verified patch, and merging is always human-approved.
9. Never read secret values into the conversation or print them in logs.
   Refer to secrets by name (e.g. `GITHUB_TOKEN`); never echo their contents.
