# Changelog

All notable changes to **agami** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `version` in `.claude-plugin/marketplace.json` and `plugins/agami/.claude-plugin/plugin.json`
is the source of truth a host installs against — bumping it is what invalidates a
user's plugin cache (see [CONTRIBUTING.md](CONTRIBUTING.md)). Each released section
below corresponds to one such version.

## [Unreleased]

### Added

- **The server decides which tool calls are one conversation, instead of asking the model.**
  `tool_calls` gains `conversation_id`, worked out when the call is recorded from the authenticated
  actor, their organization and the clock: calls by one person continue a conversation until a pause
  longer than `CONVERSATION_IDLE_MINUTES` (30), and the next call after that pause starts a new one.
  Nothing a caller sends can influence it.

  `thread_id` was meant to be this and is minted by the model, which cannot be relied on. Measured on
  one deployment: asked in prose it arrived on 2 of 10 calls and then 0 of 8; made a required
  property (0.7.1) it arrived on 9 of 9 **and collided** — a model forced to invent an id it does not
  otherwise track emits the shortest string the schema accepts, so two conversations two days apart
  both arrived as `t1` and were shown as one, with their turns merged. Handing it a server-minted id
  to echo back was ignored as well; nothing carried in prose is honoured, wherever the prose sits.
  The transport cannot supply one either — MCP removed `MCP-Session-Id` in the 2026-07-28 revision,
  and this server already runs stateless for the reason the spec dropped it.

  The rule is a judgement, not a fact: two conversations a minute apart are one row. What it
  guarantees is that the answer is the SAME every time, computed rather than remembered, and that the
  mistake it can make is bounded to one person's own back-to-back conversations — it can never merge
  two people and never join two days.

  `list_sessions` groups by it, falling back to the old `(actor, thread_id)` pairing for rows written
  before the column existed — so the Activity view stops blending two conversations that reported the
  same self-reported id, without turning history into singletons.

  `thread_id` is unchanged and still recorded; this is an addition. Deriving the value costs one
  indexed lookup per recorded call, on the connection the recorder already holds.

- **`python -m conversation_backfill`** stamps `conversation_id` onto calls written before the column
  existed, under the same rule imported from the live code rather than restated. It prints what it
  would do and writes nothing without `--apply`, because grouping rows that were never grouped is a
  judgement about what happened and belongs to a person rather than to an upgrade.

## [0.7.1] — 2026-09-04

One opt-in change, off by default: a deployment can require the `thread_id` that links a
conversation's tool calls together. Nothing about the served tool surface moves unless the flag is
set.

### Added

- **A deployment can require `thread_id`, so conversations stop fragmenting in the activity log.**
  The id that groups a conversation's calls is self-reported — the activity-log directive asks for it
  on every call and ends "Best-effort; omit if unknown" — and models take the omission. Measured on
  one deployment across two consecutive conversations: 2 of 10 calls carried one, then 0 of 8, not
  one including the first. `list_sessions` degrades gracefully (each orphan becomes its own
  singleton, so the Activity view stays audit-complete), but the conversation those calls belonged to
  is gone. Setting `AGAMI_REQUIRE_THREAD_ID` promotes the property from optional to **required** on
  every tool declaring it, which the MCP SDK enforces before dispatch; the same client then sent it
  on 9 of 9 calls. Deriving it server-side is not available — the OAuth `sid` identifies one
  authorization and survives token rotation, so it would file every conversation under one thread,
  and the streamable-HTTP transport is stateless, so there is no `mcp-session-id`. **Default off**,
  because the change cannot fail softly: a call omitting a required property returns an input
  validation error rather than a degraded record, so it should be turned on somewhere observed before
  being rolled forward. Existing deployments are unaffected until they opt in.

## [0.7.0] — 2026-09-02

The release is one feature: **golden datasets** — a way to write down questions whose answers you
have already agreed on, and then find out, on demand or in CI, whether the model still answers them.
Everything below is additive. No argument, return shape, refusal rule, receipt key or CLI flag from
0.6.x moves.

### Added

- **A golden dataset: the questions you have agreed on, and the answer key for each.** A golden dataset
  is a file of questions where an author has written the question, the SQL they accept as the answer,
  and how strictly a run has to match it. Datasets live under the profile and are read by
  `semantic_model.golden`, whose whole job is to hand a runner records it can trust — and to say out
  loud which files and cases it dropped getting there.

  Two properties hold and are the reason it can be trusted. **One bad file does not cost you the
  run:** a malformed file, or one malformed case inside a good file, becomes a finding and the read
  continues. **Nothing returned carries a filesystem path** — not on a record, not in a finding — so
  a runner downstream cannot forward a dataset location into a subprocess.

  `plugins/agami/shared/golden-dataset-shape.md` is the authoring reference, and a test parses its
  own example through the real reader, so the document cannot drift from the parser that has to
  accept it. Its hard rule against reading a sibling profile to learn the shape binds harder here
  than anywhere else: a golden dataset holds the business definitions *and* the answer key in one
  file, so a glob across profiles reads another tenant's questions together with the SQL that
  answers them.

- **Scoring is deterministic, not a model's opinion (`semantic_model.comparator`).** Given the answer
  key's result set and the one the generated SQL produced, `compare_result_sets` returns a score. It
  executes nothing, writes nothing, opens no connection, and cannot make a model call. That is what
  makes a run reproducible rather than a second opinion.

  Three decisions in it are worth knowing, because they decide what counts as the same answer.
  **Columns match on their values, never on name or position** — a correct statement that renames and
  reorders its projection is still correct, and that is precisely the case a name-matched comparator
  fails. **Cells canonicalize to a `(type_tag, value)` key**, which is not stylistic: Python collapses
  `True`, `1`, `1.0` and `Decimal(1)` into one bucket, so without the tag a boolean column would
  compare equal to an integer column of zeroes and ones. **Ordering is read from the golden statement
  alone**, parsed rather than pattern-matched, so a generated statement that drops the `ORDER BY`
  still scores order-sensitively — and an `ORDER BY` inside a subquery, a CTE, an `OVER (…)` or an
  aggregate does not make a result ordered.

- **`/agami-eval` — run a dataset and read the verdicts, failures first.** The developer surface for
  all of the above: no admin, no server, no browser. `plugins/agami/scripts/run_golden_eval.py`
  takes `--profile`, `--dataset`, `--tag`, `--rerun-failures`, `--top-k` and `--timeout-s`, and
  `--list` answers "what is there" with no database, no generator and no credentials.

  **The generating context never holds the answer key.** A model grading itself against a key it can
  see reports a pass rate that means nothing, and no assertion over the scores would reveal it. The
  generation runs in a sandboxed child process, and this is the one path in the product that does not
  generate SQL in the caller's own context — the isolation is the point. The child's flags **are** the
  sandbox and are asserted present by a test, because in that client the absence of a flag is the
  permissive default.

  **Both statements execute through the guarded chokepoint**, with an injected executor rather than a
  raw connection — so a golden run is bound by the same read-only guard, row cap and statement
  timeout as any other query, and works on every engine rather than one.

- **An exit code CI can gate on.** `0` every confirmed case passed, `1` a confirmed case failed, `2`
  no verdict could be produced. `2` means *go look at the harness*, never *the model regressed*: it
  covers a preflight refusal, a run that stopped partway, a run where every generation errored, and a
  selector that matched nothing. A run that is both incomplete *and* carries failures reports `2`,
  because sending someone to debug a model change against a run that never happened is the expensive
  mistake.

  The verdict rests on **confirmed** cases only. An unconfirmed item's answer key is one nobody has
  reviewed, and gating CI on it would fail a build over a case no human has agreed to.

- **`/agami-save-golden` — two doors into a dataset, kept apart on purpose.** A dataset that has to be
  hand-written stays empty, and an empty dataset gates nothing. The **import** door turns a CSV
  question bank into items written unverified — volume arrives here and it gates nothing. The **save**
  door writes one answer a person just accepted, with the statement that produced it and the result
  as its receipt, marked verified.

  They are separate because a spreadsheet holds questions, not the statement that answers them. An
  import that marked its own rows verified would forge the gate: the suite would fail on its own
  errors and teach people to ignore it.

  Every write is **append-only** — a write that would change an existing item renders the before and
  the after and stops for an explicit yes — and every write funnels through one function, so the rule
  holds for both doors. A write proves itself by re-reading the file through the runner's own reader
  and rolling back if that read refuses.

- **A report for a run, and a page for a dataset.** When a golden question stops matching, the useful
  artifact is the confirmed statement and the generated one read against each other, which does not
  fit in a chat message. A run now writes a self-contained HTML report with both statements side by
  side — no rows, no network — beside its machine-readable artifact, sharing one filename stamp so the
  pair is findable by name.

  The **golden dataset explorer** answers the other question: not what broke, but what the dataset
  *is* — how many items exist, how many can actually gate a run, which have rotted. Four tabs (Items ·
  Coverage · Lint · Queued) with live search, and edits queued in the page and handed back as one
  block a deterministic parser applies. **The Coverage tab is the reason the page exists:** forty
  questions that never touch the revenue metric read as coverage they do not have, and the model
  change that breaks revenue passes clean. Coverage is computed from what the statements actually
  read, so that gap is a fact rather than a judgement.

### Changed

- **A reconcile run keeps the answers it already proved.** A run that matched nine of twelve numbers
  has produced nine verified answers — a person read each one and accepted it — and then threw them
  away: the statement that produced each number lived only inside the report HTML, and the row record
  never held it. The row record now keeps the statement and the result (additive; every existing key
  stays), and after the run's summary the rows that agreed are offered once for promotion into a
  golden dataset. It writes through the save door above and nothing else. Declining writes nothing.

  Only rows that actually matched at the run's own tolerance are eligible, and a promoted item is
  written as a **band around the observed value** rather than an exact match, so it passes its own
  next run by arithmetic rather than by hope. A promotion edits the question and never re-anchors the
  statement to `CURRENT_DATE`: the band was drawn around today's value of a window that slides, so
  anchoring it would drift the item out of its own band and fail later as a false alarm.

## [0.6.9] — 2026-08-27

### Added

- **An existing password account can move onto an identity provider, keeping everything attached to
  it.** A deployment running on usernames and passwords could not switch to single sign-on:
  `_resolve_oidc_user` refuses an account not already bound to the provider, so on the day it was
  turned on every person already using the product would be locked out, with their conversations
  orphaned behind an account nobody could reach. Verified against a real account rather than
  reasoned about — it came back `None`.

  `migrate_password_user_to_oidc` keeps the row, so the id, the address and everything keyed on it
  survive. **The password is cleared**, because left in place the directory would not actually be in
  charge: revoking somebody there would not stop them signing in with a password they chose months
  ago, and nothing would show it. Guarded so it can never take an account already bound to a
  different provider — the identity-confusion guard has to survive the migration that relaxes the
  password case.

  Deciding *whether* to adopt is deliberately not this function's job. It is safe only where the
  caller has already verified the token against a pinned tenant and checked the address against that
  company's own domains; core has neither of those facts, and its consumer does.

- **`set_user_names`** fills in a display name from whatever the provider now says, so a name
  corrected in a directory reaches the product without anybody retyping it. A blank never erases
  what is there: a provider that omits a name is silent rather than authoritative, and treating
  silence as an instruction to delete would wipe a name an administrator typed in.

### Changed

- **`http://localhost` is accepted for `PUBLIC_BASE_URL`.** TLS is still required everywhere else,
  for the reason it always was — a browser drops a `Secure` cookie over http. Browsers make a
  specific exception for loopback, which is a secure context by their own definition, so nothing
  that rule protects is lost. It is also the only thing that works: identity providers permit a
  plain-http redirect only on loopback, so a developer signing in against a real provider from their
  own machine had no https option to choose and could not run the server at all. The host is parsed
  rather than prefix-matched, because `http://localhost.example.com` is somebody else's domain.

- The sign-in consent line now reads **"Allow `<client>` to connect to your data"** rather than "to
  access your data". What is granted is a connection the person can see and remove, not a handover
  of the data itself, and on a screen read in two seconds before deciding that is the whole
  decision.

## [0.6.8] — 2026-08-27

### Fixed

- **A database connection the server closed is now reopened, instead of poisoning the process until
  it is replaced.** `Store` opened one Postgres connection at startup and held it for the life of the
  process. A deployment that sits idle for about an hour has that connection reaped from the server
  side, and nothing told the process: `execute` called `conn.cursor()` with no liveness check and
  there was no reconnect path anywhere in the package.

  The instance was then poisoned for as long as it lived — **every** request, not only the ones that
  touch data, answered in about three milliseconds having attempted no round trip at all. The three
  milliseconds are the tell. A downstream deployment hit this four times in four days, one to two
  hours each, across two revisions; thirty days of that service's error logs contained 123 tracebacks
  and every one was this.

  It fails closed on all traffic because the first thing a request does is resolve the caller's org,
  and that read goes down the dead connection — before authentication, before routing. Somebody who
  has just signed in successfully sees a connector error, which reads as a login failure and gets
  reported as one.

  `Store` now keeps the URL it connected with and retries once when the connection is gone. Three
  things bound that retry, and each of them was a real defect in an earlier draft rather than
  caution for its own sake:

  - **It refuses while an uncommitted write is outstanding.** Callers write two rows that must land
    together — a role change and the audit line naming who granted it. If the connection dies between
    them the first is already lost, and reconnecting silently would let the second commit **alone**:
    an audit line for a grant that never happened, which is exactly what that line exists to make
    impossible.
  - **A read does not count as an outstanding write**, and that distinction is the difference between
    fixing this and appearing to. The path this exists for reads five tables in a row and never
    commits, because there is nothing to commit. Counting reads would have reconnected once and then
    refused for the life of the process.
  - **`OperationalError` alone is not enough to act on.** It is a broad base class — a cancelled
    query, a statement timeout, a full disk. Retrying on any of them re-runs the statement, and for
    the first write of a transaction that lands the row twice. Only the driver marking the connection
    closed counts. `InterfaceError` needs no such test, since the driver raises it only on a
    connection it has already closed.

  It also refuses while a session-scoped advisory lock is held, so a reconnect part-way through a
  migration cannot continue without the lock and let a second instance apply the same files.

  `Store.rollback()` is new, and callers that reached past it into `store.conn.rollback()` now go
  through it — a rollback that bypassed it would leave the connection marked mid-transaction forever
  and unable to reconnect again.

## [0.6.7] — 2026-08-18

### Added

- **A curated example keeps the same id across deploys, and the caller can see it.** `prompt_example`
  has had an `id` column in its primary key since the table existed, and it was unusable three times
  over: an example with no `id` in its YAML got a minted uuid4, nothing wrote that back, so the next
  deploy minted a different one for the same example — and the read selected `question, doc`, never
  the `id`, so no identity reached a caller even where one existed. Across a real deployed model of
  16 subject areas and 46 curated examples, not one carried an id.

  So nothing could name an example, count one, or say whether the one returned yesterday is the one
  returned today. `agami-save-correction` cannot tell a correction that created an example from one
  that should have replaced it; ranking returns a subset and which subset is unrecordable; the library
  only grows, because no two observations of an example can be tied together.

  The id is now **derived from the example's own content** — `question` and `sql`, each NUL-terminated,
  SHA-256, twelve hex characters, the construction `compute_model_hash` already uses. Derived rather
  than minted, because a minted id changes every deploy; derived rather than authored, because
  authoring gives the id two homes that can disagree and needs a pass over every deployment. An
  authored `id` in the YAML still wins, which is the escape hatch for an example deliberately filed
  under two subject areas — those otherwise resolve to one id, and the first now wins rather than the
  second aborting the deploy.

  `select_examples` returns it, and `example_by_id` reads one back, scoped by org and datasource like
  every other read in the module. The derivation is one-way, so a consumer holding an id needs the
  lookup for the id to be worth having.

- **An agent can say what it based a query on, and why.** The activity log records what ran and two
  self-reported columns carrying the caller's framing of the question. What it never recorded is the
  reasoning in between: why that table, when the model declares three that look similar; why that
  join, when it declares no relationship between those two tables; which curated example this
  followed, or whether there was none. An operator could see that a query was wrong and not where the
  reasoning went wrong — and a query against the wrong table is indistinguishable, in the log, from
  one against the right table.

  `execute_sql` takes an optional `basis`: a list of `{kind, ref, why}`, recorded on `tool_calls` and
  rendered in the activity log beneath the statement it explains. Absent is the ordinary case, the
  field is not `required`, and a client that has never heard of it is unaffected.

  **The property is declared as a bare array, and that is a correctness constraint rather than a
  simplification.** The MCP SDK validates arguments against `inputSchema` before the handler runs, so
  a `maxLength`, `maxItems` or `enum` there does not trim a bad entry — it refuses the whole call and
  takes the caller's answer with it. The kinds are advertised in the description and enforced at the
  boundary, where an over-long `ref` or an unknown `kind` costs its own entry and nothing else. The
  stored value says when it was cut, so a reader never takes a truncated list for the whole claim.

  Self-reported, like its neighbours: core records the claim and adjudicates nothing. Whoever holds
  the receipt can check `ref` against the SQL themselves.

## [0.6.6] — 2026-08-14

### Added

- **The activity log can name the execution it ran.** `tool_calls` records that a tool ran and
  `query_executions` records what the warehouse was asked and what the guard decided; both are
  written for every `execute_sql`, they are 1:1, and nothing joined them. `018`'s own comment
  recorded the gap — *"there is no key between them (`Envelope.audit_id` is `query_executions.id`,
  which `tool_calls` does not carry)"* — and chose to duplicate a column rather than add one.

  That holds for a single sentence-shaped field. It stops holding once anything has to be attached to
  a **statement**: `correlation_id` names the turn, one turn runs several statements, and no column
  told them apart. A reader wanting per-statement facts had to guess, and the only material to guess
  with was the agent's own framing text — which repeats whenever a turn retries a sub-question, so it
  mis-attributes in silence.

  `tool_calls` now carries `audit_id`, and the value is one the caller already received: `_emit`
  stamps it onto every envelope, and `query_executions.id` is that same id. The column reconciles
  nothing and mints nothing.

  It is read off the body `record_tool_call` already parses, on `ok`, `refused` and `failed` alike —
  a blocked or broken call is the one an auditor most wants to trace. A caller that hands over no
  body can state it instead, like the outcome fields beside it. NULL is the ordinary case, not a gap:
  a schema read or a datasource list runs no statement and has none.

### Fixed

- **A client asking what this server looks like gets the icon, not a 401.** `/favicon.ico` was not
  merely absent, it was gated: the bearer middleware challenges anything off its public list before
  routing can 404 it, so the one unauthenticated request a client makes to learn what a server looks
  like came back as an auth prompt. An MCP client showing a server in a connector list has no
  credentials to offer for an icon fetch, and a deployment that redirects `/` elsewhere leaves this
  the only path left to ask on — so such a server drew a letter avatar beside servers that answered.

## [0.6.5] — 2026-08-13

### Fixed

- **A model whose subject area joins one pair of tables twice now loads.** It did not before: each
  relationship row was keyed by its two table names, and that key is the table's primary key — so the
  second join collided with the first and the INSERT raised part-way through the write. The failure
  was total rather than partial. Nothing commits, so the deployment ends up serving **no semantic
  model at all**, and the error names a column tuple rather than the join that collided.

  Self-referencing tables make this ordinary rather than exotic: an employee row pointing at another
  as its manager, and again as its mentor, is one pair of tables and two genuinely different joins.
  Two `on:`-expression joins between one pair, and same-named tables living in two schemas, failed
  the same way.

  The key now carries the relationship's position within its subject area, so a collision is
  impossible for any model that validates rather than impossible for the shapes somebody enumerated
  — which is what the two previous versions of this key each attempted, and each missed. Nothing
  reads the value (a relationship is rebuilt from its stored document), and a redeploy replaces a
  datasource's rows wholesale, so models written under the old key need no migration and no backfill.

## [0.6.4] — 2026-08-12

### Added

- **The claim page says whose account it is.** Somebody following a setup or reset link was asked to
  choose a password with no indication of which account it belonged to. A link is shared out of band,
  so the person holding it may have been sent the wrong one, or two of them — and now that a link can
  REPLACE a working password rather than only set a first one, following the wrong one silently locks
  somebody out of their own account with nothing on the page to warn them. It discloses nothing new:
  whoever holds the link already holds a token naming that account.
- **The MCP surface states what the server already does.** Four capabilities shipped working and
  undocumented, and a silent feature is worse than a broken one — a broken one gets reported.
  `execute_sql` returns three statuses and described two; the third carries the whole database-error
  channel with a `kind` from a declared enum, which tells an agent whether a failure is repairable
  from the schema it already holds or whether retrying is pointless. Documented, so it can be used.

### Fixed

- A password refused for being **too long** said "use at least 8 characters" — the same message as
  one refused for being too short, and advice that makes the problem worse the more carefully it is
  followed. Each bound now names itself.
- Seven store-step tests resolved the profile from the developer's own `~/agami-artifacts` rather
  than from the temporary directory they had set, so they compared against a value from a file the
  test never mentions. CI has no such file, so they passed there and failed only on a machine that
  had run the CLI.

## [0.6.3] — 2026-08-11

### Added

- **An administrator can give a colleague a new password.** The setup link works exactly once —
  claiming an account flips it out of `pending`, and every later use is refused — so an administrator
  could create somebody's account and then never help them back into it, and with no mail path there
  is no self-service reset either. `/claim` now carries a second purpose: `setup` still means a
  pending account choosing its first password, and `reset` means a claimed account being given a new
  one because an administrator asked. The two are matched against the state of the account rather
  than trusted from the link, so neither can do the other's job.
- A reset link is **single-use**, by a mechanism the setup link does not need: a reset leaves an
  account exactly as claimed as it was, so nothing about it would change and the link would work
  forever. The link is bound to the credential it was minted against, and that credential is a
  condition of the write — so the first successful reset retires it, a password change by any other
  route retires it, and two simultaneous uses cannot both succeed.
- A reset **ends the sessions running on the old password** by revoking that person's refresh
  tokens. Stated precisely because a security control believed to cover more than it does is worse
  than a narrow one: a session already holding an access token lasts until that token expires, and
  the `/admin` console cookie and any outstanding authorization code are untouched.
- A reset can never give a password to an identity that signs in through a provider, and can never
  revive a switched-off account.

### Changed

- The claim page is worded for what the link is for — somebody with an account is not told to
  "finish setting up" — and `setup_page_html` / `setup_done_html` are now `claim_page_html` /
  `claim_done_html`, which take the purpose.
- Password hashing on `/claim` runs off the event loop. It is deliberately slow and memory-hard, and
  this is a public endpoint, so hashing inline stalled every other request in flight.
- `get_datasource_schema` answers **within the scope the caller declares**, and the never-hide
  guarantee is now stated relative to that scope: within it, nothing is hidden — the scope's own
  metrics plus the cross-area bucket. A new `area` parameter narrows to one subject area
  (completing the set `execute_sql` and `get_prompt_examples` already had); `dataset_names`
  narrows to those tables. `query` and `metric_names` rank and select detail and do **not** scope,
  because narrowing on something the caller never declared is silent deprivation. The response
  echoes the `scope` it resolved.
- The appended domain context no longer repeats the subject-area listing that the same response
  already carries as structured `subject_areas`.

### Fixed

- A table-scoped `get_datasource_schema` call returned columns and **no way to join them**. It had
  resolved the relationships and metrics for those tables and discarded them, while the tool's own
  description promised both. It now returns them, with each metric carrying the binding for the
  deployment's engine — so one call carries the columns, the joins and the declared expression.
- **The instructions named a metric field the payload does not send.** Both instruction surfaces
  told the agent to reuse a metric's `calculation`/`bindings` VERBATIM; the payload key is
  `binding`, singular, already resolved to this deployment's engine. An agent told in capitals to
  reuse a field it never receives hand-rolls the SQL instead — and hand-rolled SQL does not reduce
  to the declared binding, so the receipt reads `unmatched` on a column that does compute the
  metric. The text and the payload are now pinned to each other by a test derived from the
  projection itself.

## [0.6.2] — 2026-08-09

### Fixed

- **An omitted `datasource` names the org's real datasources instead of one nobody has.** #216 (also in
  this release) removed the invented `'default'` for a deployment serving exactly ONE datasource;
  serving several, resolution still fell through to that literal and the caller heard
  `no such datasource: default` — so the one-datasource case and the several-datasource case are fixed
  together in this release, and nothing in 0.6.1 or earlier ever consulted the store to resolve a
  profile at all.

  Two audiences read that sentence and both were misled: an administrator sees a name no customer has
  and concludes their data has gone missing, and a model sees a failed lookup and invents another name,
  because the tool's own description told it a default existed. So the description is fixed as well as
  the refusal — all three tools now say what is true on each install and point at `list_datasources`.
  An omitted argument gets the catalogue back (`datasource_required`); a NAMED one that does not exist
  is still told so; an empty string counts as omitted, matching `resolve_profile`'s own
  `if explicit:`. (#218)

- **A CASE predicate is not what a SUM aggregates, and a shared expression names no metric.** (#215)

### Changed

- **The tool surface reports what it checked, and accepts what it advertises** — nine findings from a
  connector audit, all one shape: the server stated something it had not established, or named a field
  the agent could not reach. `model_present` dropped from the served listing (it was the literal `True`);
  `resolve_profile` consults the store; a hosted server no longer claims "All execution is local";
  `bindings` is actually sent; `area` reaches `get_prompt_examples`; `list_datasources` returns
  `description`; `for_questions_about` dropped from the payload. (#216)

  **Released as a patch deliberately, and it is the one judgement call in this version.** #216 REMOVES
  fields from emitted tool payloads, which a consumer reading them would notice — normally a minor bump
  under 0.x. It ships as 0.6.2 because every field removed was provably dead: `model_present` could only
  ever be `True` on that path, and `for_questions_about` had no writer anywhere in the product. A
  consumer reading either was reading a constant or an empty list. The field stays DECLARED on the model
  format, so no model on disk fails to load.

## [0.6.1] — 2026-08-08

### Fixed

- **A refusal's own sentence reaches the audit row again, so a console reader can act on it.**
  `tool_calls.refusal_detail` and `tool_calls.refusal_remediation` — the two columns added in 0.6.0 so
  that a row says what the decision was made against and what you were told — were **NULL on every
  refusal a real deployment recorded**. The rule was recorded; neither sentence was.

  The cause was the coherence rule that shipped beside them. It cleared both whenever a caller
  *stated* the outcome rather than leaving it to be parsed out of the response body, reasoning that
  such a caller is not offering these sentences. That is true of a caller who **contradicts** the
  body, and false of every real one: the served MCP transport states the outcome for every tool that
  speaks the Envelope, which `execute_sql` does, and a consumer's own sink states what it observed
  with no body to hand over. So the sentences survived on exactly one path — a caller that hands over
  a body and states nothing — and nothing in production takes it.

  They are now kept when the stated outcome **agrees** with the body, and dropped when it does not.
  What the rule guarded is unchanged: a stated success has no refusal to explain, and a stated kind
  naming a different failure is describing something these sentences are not about. No consumer
  change is needed — both surfaces already state the rule itself.

  The tests are worth naming, because they were the real defect. Every test that asserted these
  sentences were **present** drove the recorder the way no production caller does — handing over a
  body and stating nothing — so they went on passing while every path the world actually takes
  recorded nothing. A test can be demonstrably wired to the line it covers and still say nothing
  about whether that line is reached the way the world reaches it. The new test that states an
  outcome agreeing with the body is the one that fails against the shipped behaviour.

## [0.6.0] — 2026-08-08

### Added

- **A server can now run with the semantic-model pass off, and always says so
  (`AGAMI_GOVERNANCE_ENFORCED`, ACE-101).** The model-scoping checks refuse on facts about *our*
  parser and *our* model resolution rather than about your SQL, so a dialect drift or a model that
  will not resolve could refuse every query on a server until an operator intervened, and there was
  no way to bring that server up without them. There is now one switch, read per request so flipping
  it takes effect on the next query with no redeploy.

  **It is off by default,** which means a fresh server does not enforce table scope, column scope,
  the `SELECT *` ban, or the engine-mismatch check until you turn it on. With it off, a query may
  read anything the connecting role is granted, including columns you excluded from the model, and
  may enumerate your schema through catalog relations. Turn it on once the checks have been
  validated against your data; see the [self-hosting reference](docs/self-hosting.md) and
  [SECURITY.md](SECURITY.md).

  **What the switch cannot reach:** the read-only guard, the dangerous-function guard, the statement
  timeout and the row cap. Those are composed outside the pass and enforce in both postures, as does
  the read-only database role. And nothing ever claims the checks ran: every answer and every audit
  row carries "the semantic-model checks are turned off in this deployment", on all five receipt
  sections, with no findings attached.

  The server logs one warning at startup naming the variable and the exposure whenever it boots with
  the pass off.

- **The receipt now tells you which of a table's declared filters your statement actually applied
  (ACE-099).** Declaring `default_filters` on a table has never applied them to your SQL, and since
  the filter injector was removed nothing reported on them either — so `SELECT COUNT(*) FROM orders`
  returned every row where it once returned the undeleted ones, and nothing on the answer said the
  number meant something different from what the model says the table means.

  Each entry in the receipt's `tables` section now carries `filters`, one `{expr, status}` per
  declared filter, with `status` one of `applied`, `omitted` or `undetermined`, plus a `scope`
  naming where in the statement that reference sits.

  The determination is **per reference, not per table**, which is the part that makes it worth
  trusting. A filter satisfied inside a CTE and absent from the outer query is two different answers
  about the same table, and reporting one verdict for both is what made the old injection unsafe:

  ```
  WITH recent AS (SELECT id FROM orders o WHERE o.status != 'cancelled')
  SELECT o.id FROM orders o JOIN recent r ON o.id = r.id
  ```
  ```
  orders  scope=cte:recent  filters=[{expr: "o.status != 'cancelled'", status: applied}]
  orders  scope=main        filters=[{expr: "o.status != 'cancelled'", status: omitted}]
  ```

  `applied` means the declared predicate is one of the top-level `AND` conjuncts of that reference's
  own scope; extra conditions beside it do not weaken that. Anything the check cannot stand behind
  is `undetermined` rather than a verdict — a predicate on the same column that is not identical, one
  reachable only through an `OR`, one that only appears in an outer join's `ON`. Only an outright
  absence is `omitted`, because a confident "you left this out" that turns out to be wrong is worse
  than saying nothing. **An omitted filter is never a refusal**: whether it matters depends on the
  question, which only you have.

  The shipped sample declares one (`orders`: `status != 'cancelled'`), so this is visible the first
  time you run it.

### Changed

- **The receipt now reports on every number your query computes, not just the ones with a problem
  (ACE-060).** The `aggregates` section listed findings, so a total no join had multiplied produced
  no entry at all — and an entry that is absent looks exactly like a check that never ran. It also
  named the measure *table*, so a query computing two numbers over one table told you a join
  multiplied "orders" and left you to work out which of your two numbers it meant.

  There is now one entry per aggregate, saying the aggregate as parsed, whether a join multiplies
  the rows behind it, and which join does. A number nothing multiplied **says so**, which is the
  point: reading "not multiplied" beside a total is the difference between a clean answer and an
  unchecked one.

  An aggregate whose reads could not be resolved reports `undetermined` rather than clean.
  `COUNT(*)` is the case that matters: it names no column, so nothing tells us which table's rows it
  counts, and a fan-out around it is invisible to the check. Reporting that as clean would put a
  clean bill of health on the one number a join had multiplied.

  The section's `undetermined` line is now composed per query from what *that* query left open, so
  it can finally be empty. It used to carry a sentence on every answer, including "whether this is a
  problem depends on the question" — true of every answer forever, and the reason the section could
  never say "checked, and complete". A trap is still reported and still never refused.

- **The audit row now says what the decision was made against, and what you were told (ACE-098).**
  A row recorded the verdict — `ok` / `refused` / `failed`, and for a refusal which rule under which
  reason — but nothing about the basis for it, so nobody could take a row and check the decision
  again. Three columns close that: `detail` (the refusal's own sentence, which is where "which bound
  fired and what it was set to" lives, since the statement timeout and the row bound share one rule),
  `receipt` (everything the trust receipt reported, including its `undetermined` markers), and
  `model_version` as a column you can filter on.

  A test now takes those rows and **re-derives each refusal with no database connection at all**,
  matching what was recorded. That is the check that tells whether the fields are sufficient rather
  than merely present. The two runtime bounds are exempt and stay exempt: whether a statement
  outruns its budget is a property of the run, not of the SQL, so it is not reproducible offline by
  anyone.

  Also, the **tool-call log now reads the verdict rather than re-reading the answer**. It used to
  parse the response body to work out whether a call failed and why, which made the audit trail
  depend on the wire format; it now takes the classified outcome directly.

- **A self-hosted server that cannot record a query no longer runs it (ACE-097).** Recording was
  best-effort in three places, and two of them were silent, so a deployment could execute SQL
  against your database and keep no record of having done so with nothing anywhere saying the
  record was lost.

  On a **server** (one with `AGAMI_DB_URL` or `APP_DATABASE_URL` configured), the audit store is now
  checked before the statement runs. If it cannot be opened the call is refused, with
  `rule: audit_unavailable` and a remediation naming the operator action, and the statement never
  reaches your database. If the store was reachable at that check and the write fails afterwards,
  the call fails rather than returning an answer whose statement left no trace. The connection is
  read-only, so nothing was changed and re-running costs only the round trip.

  **Local single-player use is unchanged.** With no database configured there is no audit store to
  reach: the log is a local jsonl file, a write failure is still logged and never breaks your query,
  and a read-only artifacts directory cannot stop you asking questions.

  For operators this is an availability change, and a deliberate one: a briefly unreachable audit
  database now produces refusals rather than unrecorded answers.

- **A result too large to return is refused, not trimmed.** A query whose result exceeded the
  deployment ceiling (`AGAMI_SQL_MAX_ROWS`, default 1000, unchanged) used to come back cut down to
  that many rows with a flag saying so. It now comes back as a structured refusal carrying no rows.

  The trim was unsound before it was anything else. Without an `ORDER BY` a SQL result has no
  defined prefix, so what you got was whichever rows the engine happened to emit first — different
  between runs, different between engines, and presented as the answer. It was not a smaller version
  of your result; it was an arbitrary sample of it.

  The refusal tells you which fix applies to the statement you sent, because the wrong one is worse
  than none: a row listing should be bounded with a `LIMIT` and an `ORDER BY`, while an aggregate
  should have its grouping narrowed or a filter added — putting a `LIMIT` on a grouped result drops
  groups, and the breakdown you get back reads exactly like a complete one.

- **A `#` is no longer mistaken for the SQL it hides, and is now refused wherever it appears
  outside a string (ACE-096).** `SELECT a FROM t # DROP TABLE t` came back as *"keyword 'DROP' is
  not allowed"*, and `... # note; more` as *"multiple statements are not allowed"* — both refusals
  of valid MySQL, and both naming a fix that would not have helped, because neither the `DROP` nor
  the second statement was ever going to run.

  The guard reads every statement with one grammar and no engine, and `#` means four different
  things across the engines it speaks: a line comment in MySQL and MariaDB, the `#` / `#>` / `#>>`
  operators in PostgreSQL, a temp-table prefix in SQL Server (`#tmp`, `##global`), and an ordinary
  character inside a backtick- or bracket-quoted identifier. It cannot tell them apart, so it now
  declines to pick a reading and says which ambiguity it hit — the same call it already makes for
  a bare `--x`.

  **This is a widening: all four shapes ran before and are refused now**, not just the comment.
  If you use jsonb path operators, SQL Server temp tables, or a `#` inside a quoted identifier,
  those statements will start coming back refused. It is the accepted cost of one grammar; the
  other direction lets a trailing `;DROP` ride through inside text the guard decided to ignore.
  Rewrite a `#` comment as `-- ` or `/* … */`; a `#` inside a name has to be spelled another way.

- **The trust receipt is five sections, and each one says what it did NOT establish (ACE-088).**
  Every answer's receipt now carries `columns`, `tables`, `joins`, `aggregates` and `assumptions`,
  always all five, each an object `{items, undetermined}` beside the `model_version` pin.
  `undetermined` is a plain sentence naming what that section did not check; it is `null` only when
  the section is complete. **This is the point of the change**: before it, a section nobody had
  checked and a section that found nothing were both the empty list, so silence read as clean.
  Aggregate fan-out is the live example — the receipt now states, where you read the answer, that
  whether a join multiplies the rows an aggregate is computed from was not checked, instead of
  shipping an empty section you would read as "no problem". `assumptions` is the section that most
  often has nothing to admit, and it earns its `null` rather than assuming it: it lists at most
  three AI-written column meanings and counts any beyond that onto its own marker, because a
  truncated list under a null marker is a positive claim of completeness.

  The receipt also rides on **every** status, not just `ok`. Every non-ok body carries the bounded
  form — the caller's own identifiers and, per reference, whether the model declares that name;
  nothing else about the model. `tables` is now **one entry per reference** rather than per table,
  so a table read twice is listed twice and a reference the model does not declare says so. A name
  the statement defined for itself — a CTE, including one that shadows a real table — is not a
  declared table in ANY section: it borrows no row estimate, no schema-qualified column label and
  no model-written column meaning, in every spelling of a column reference.

  **Breaking for anything reading the old flat keys.** `tables_used` → `tables.items[]`;
  `relationships` → `joins.items[]`; `metrics` → the `columns.items[]` entries whose `metric` is
  non-null; `assumptions` → `assumptions.items[]`; `warnings` → derive it from `joins.items[]`
  filtered to `review_state != "approved"` (a review state can be counted and linked back to its
  join; a pre-rendered sentence could only be printed); `named_filters` and `sql` are gone (nothing
  ever produced the former, and the statement is on the response body). The HTML report, the MCP
  server instructions, `render_chart.py` and the `agami-query` skill all move with it, and
  `render_chart.py` now **rejects** a receipt that is missing a section rather than silently
  rendering nothing for it.

- **The answer report renders the whole receipt.** The provenance panel draws every section with its
  marker, lists tables per reference (an undeclared one explains itself instead of showing blanks),
  and adds the columns the statement referenced. The unreviewed-join banner is derived from the join
  review states, so it can count them and name each one. The unapproved-metric banner and its
  Approve / Change write-back are unchanged in behaviour. Two long-standing display bugs fixed while
  repointing: an unreviewed entry read "confidence ?" to every user (the phrase tested `confidence`
  as a number; it is a label), and a named-filters block that no producer ever filled is deleted.

### Removed

- **`max_rows` is no longer an argument to `execute_sql`,** and `--max-rows` is gone from the
  command line. It could only ever *lower* the deployment ceiling, so the one case where a caller
  knows better than the operator — wanting more data — was the case it could not serve. Ask for the
  rows you want in the statement: `LIMIT 200` says what it means to everything that reads it.

- **`truncated` is no longer a field on a successful result.** With an oversized result refused, it
  could only ever be `false`, and a field that is always `false` is one a client can only branch on
  wrongly.

- **Agami no longer rewrites your SQL to fix a fan-out join.** A query that aggregated a measure
  across a one-to-many join, touching the many side nowhere but the `ON` clause, used to have that
  join silently dropped and the rewritten statement executed in place of yours. Your statement is
  what runs now, byte for byte — comments, whitespace and quoting included — and that is asserted
  rather than assumed.

- **Four correctness checks stopped refusing.** A fan trap, a chasm trap, a `SUM` of a rate or an
  identifier, and a `SUM` of a balance across time were all refused. They **return a result** now,
  and what the check found rides on the answer's receipt, under `aggregates`.

  This is the point of the change rather than a relaxation of it. Whether a multiplied total is
  *wrong* depends on what you asked: the same statement is wrong for order revenue and right for
  line-item exposure. The check has your SQL and your model and never your question, so it describes
  what it found and leaves the judgement to you — or to the assistant, which does have the question
  and is asked to say out loud when it restructures a query because of a finding.

  A statement that trips two conditions now reports both. The old code stopped at the first, so a
  query that both fanned out *and* summed a rate was reported as having one problem.

- **`sensitive` is a description, not a gate.** Marking a column `sensitive` no longer blocks
  projecting it. The answer's receipt reports which sensitive columns it projected, under
  `columns`, and the authoring guidance asks the assistant to prefer aggregates and to say when it
  did project them.

  **If you relied on this to keep values from coming back, read this.** The gate was never a
  boundary: it inspected the projection list and nothing else, so `WHERE email LIKE …` always
  answered the same question one bit at a time. What it bounded was the *rate* of that, which is an
  access policy, and Agami holds none of its own — it reads exactly as the connecting database role
  reads. Two things do enforce, and neither changed: a column left out of the model is out of scope
  and any statement naming it is refused, and the connecting role's grants and your warehouse's
  masking policies apply as they always did. If a value must not come back, exclude the column from
  the model or make sure the role cannot read it.

- **The `model_safety` refusal rule is gone.** It stood in for two branches that refused without
  naming a rule. Both branches went, so every refusal now names the gate that chose it. A consumer
  keying on `refusal.rule == "model_safety"` will stop matching, which is the point.

- **The `GovernancePolicy` port and the `Adapters.governance` field (ACE-095).** `GovernancePolicy`,
  its `GovernanceVerdict` value type, and the `WarnOnlyGovernancePolicy` default adapter are gone,
  along with the `governance` field on `ports.Adapters`. The port was declared but never called: no
  core call site ever evaluated it, so removing it changes no behaviour and touches nothing on the
  `execute_sql` enforcement path. `Adapters` is public API, so a consumer that constructed it with
  `governance=...` drops that keyword; the remaining ports (`ActivitySink`, `OrgResolver`,
  `AuthProvider`, `Executor`) are unchanged. **`Adapters` is not keyword-only, so a consumer that
  built it POSITIONALLY must also re-check its call**: the 4th positional slot was `governance` and
  is now `executor`, so `Adapters(sink, resolver, auth, my_policy)` still constructs but binds the
  policy as the executor and fails later at query time with `AttributeError: 'MyPolicy' object has
  no attribute 'execute'`. Construct `Adapters` by keyword.

### Fixed

- **Updating the plugin now updates the library it runs on.** The plugin's own files are refetched on
  every version bump — the cache directory is the version — but the pip-installed `agami-core` was
  not, and the loader prefers that installed package over the fresh copy bundled beside the scripts.
  The readiness check only asked whether the library *imported*, which a stale one does perfectly
  well, so updating the plugin left new skills running against an old library with nothing anywhere
  saying so.

  This release is the first where that combination breaks outright rather than drifting quietly: the
  receipt below is five sections, and the chart renderer rejects a receipt shaped the old way, so
  every charted query would have failed on `receipt.columns is missing` until the library was
  upgraded by hand. The launcher now compares the installed distribution against the plugin's own
  version and reinstalls with `--upgrade` when it is behind. A library at or above that floor is left
  alone, so this costs nothing on an ordinary run, and a source checkout with no distribution
  metadata is not disturbed.

- **Catalog and dictionary reads no longer hit the row cap that exists to bound your queries.** The
  executor refuses (never truncates) any result over `AGAMI_SQL_MAX_ROWS`, default 1000. That bound
  is sized for a question someone asks; a *catalog* read exceeds it on schema size alone. The visible
  symptom was `sm enrich-metadata` dying on any platform whose data dictionary is a real table
  (`RuntimeError: … "rule": "resource_limit"`), but the quieter cases were worse: the bulk
  `information_schema.columns` read behind `sm discover`, and the table/foreign-key reads behind
  `sm introspect`, discard a refused read instead of reporting it — so on a wide catalog they
  degraded to one round-trip per table, or produced a model with no join graph, and said nothing
  about either.

  The connect skill now runs those three commands with a raised cap and **tells you it did, along
  with your unchanged query-time cap**. Nothing about the bound on an ordinary question changes: a
  query returning more than the cap is still refused rather than quietly truncated, and no new lever
  is reachable from a generated query.

- **The guard now reads your SQL in your database's own grammar, and refuses what it cannot read
  (ACE-079).** Every model-scoping check decided by parsing the statement, and every one of them
  parsed in a generic SQL dialect rather than your engine's. On MySQL, BigQuery, Databricks and SQL
  Server that is not a subtlety: a backtick is not an identifier quote in the generic grammar, so
  `` SELECT `ssn` FROM `customers` `` parsed to **no tables and no columns**. The scope checks were
  not bypassed by a trick — they inspected the tree, found nothing to object to, and passed. So on
  those engines a query could read any table in the database regardless of what your model declared,
  and the trust receipt reported no tables read, which made the answer look clean.

  The error posture was the other half. `error_level="ignore"` was not a lenient setting, it was no
  setting at all: sqlglot compares that argument against enum members, so a string matched no branch
  and every parse error was silently discarded, leaving a truncated tree that read as valid. Both
  halves are fixed together, because either alone leaves a hole.

  Four situations are now refused rather than run blind, each with the next step it actually needs:

  - the datasource does not say which engine it runs on (undeclared, unmapped, or two connections
    disagreeing) — `model_unavailable`, and the fix is the operator's: declare
    `storage_connections[].storage_type`. It does not invite you to retry the query, because no
    rewrite of the query helps.
  - the statement does not parse in that engine's grammar — `unparseable`, and you can re-emit it.
  - a double-quoted token on a backtick-quoting engine, which means a column under `ANSI_QUOTES` and
    a string literal otherwise. The server setting is not visible to the guard, so rather than guess
    it asks for the statement in the engine's own quoting.
  - the statement parses, reads from something, and resolves to no named table at all — `unscopable`.
    A backstop that does not depend on the engine map being complete.

  A model that declares one engine while its credentials connect to another is also refused
  (`engine_mismatch`): those are two independent pieces of configuration, and a mismatch means the
  statement was checked against the wrong grammar.

  **If your model does not declare a `storage_type`, queries against it now refuse** with the
  message above. This is deliberate: a datasource whose engine is unknown cannot be governed, and
  the alternative was to keep parsing it in a grammar no engine uses.

### Contract changes

- Receipt `tables` items carry an **arm ordinal on `scope`** (ACE-043). When a reference's scope is
  one of two or more arms of a `UNION` / `INTERSECT` / `EXCEPT`, its label gains a trailing 1-based
  `#<n>`: `main#1`, `main#2`, `cte:recent#2`. A plain `SELECT` and a single-arm CTE body are
  unchanged and carry no suffix, and `subquery` never takes one. **If you branch on
  `scope === 'main'` or `scope === 'cte:x'`, that branch stops matching inside a set operation —
  strip the ordinal with `scope.replace(/#\d+$/, '')` (or `scope.rsplit('#', 1)[0]`) and branch on
  that.** Split from the RIGHT, not the left: the CTE-name half is caller-written text, and it is
  only sanitization to an identifier alphabet excluding `#` that keeps a left split working today. The ordinal is the arm's position in the SQL, which
  is not the order of this list: items are in parse-walk order, so a capped receipt can list
  ordinals that are neither contiguous nor monotonic, and the largest one is not the arm count.
- Receipt `tables` items gain **`scope`** and **`filters`** (ACE-099). `ref` is unchanged and is
  still a string. A `refused` or `failed` receipt is unchanged too — it carries `{ref, declared}`
  and neither new field, because a declared filter names the columns and literals the model author
  wrote and a refusal is the one outcome a caller can provoke on purpose.
- `tables.undetermined` is now **`null` when the section is complete** (ACE-099). It used to be a
  fixed sentence on every receipt saying the filter accounting was not done. It now names only what
  was genuinely not established — references whose filters could not be accounted for, references a
  shadowing CTE name stopped resolving, and the count the reference cap dropped. If you branch on
  this field being present, that branch changes meaning: present now means something really is
  missing.
- `sm receipt --applied-filters` is **gone**, and the receipt no longer emits a top-level
  `default_filters_applied` key (ACE-099). Nothing had produced either since the filter injector was
  removed; the fact lives in `tables.items[].filters` now, in one shape rather than two.
- `sm prepare` returns `{sql, findings, units}` and **always exits 0**. It previously returned
  `{action, risk, sql, units, reason}`, or exited 1 with a refusal. It runs the reporting checks,
  not the refusing gates — do not pair it with `--no-safety`.
- `sm preflight` returns `{findings: [...]}`, replacing the single
  `{risk, action, reason, suggestion, triggering_joins}` verdict.
- The receipt's `aggregates` section can now be non-empty, and its `undetermined` sentence changed:
  it says the checks ran and names what they still do not reach, rather than saying the check does
  not happen. `columns` items may carry `sensitive: true`.
- The `{"error": {"kind": "preflight_refused"}}` and `{"kind": "sensitive_columns"}` diagnostics no
  longer appear on stderr. Every refusal is a single JSON object on every path.
- A refused `SELECT *` reports `reason: "undetermined"`, not `reason: "out_of_scope"`. The refusal
  and its message are unchanged; only the reason moves. If you route, count or alert on `reason`,
  this row changes bucket.

### Docs

- **Read-only datasource role is now a stated deploy obligation, not a suggestion (ACE-036).** The
  self-host guide and `readonly-grants.md` now frame the SELECT-only role as the **required**
  `DATASOURCE_URL` posture for a deploy (single-player stays *recommended*), spell out the app-role vs
  operator/owner-role split, and clarify that the role guarantees integrity/confinement but **not**
  availability/recon — bounding runaway work and recon is app-side, and the docs now state which of
  those bounds exist today (the result-row cap) and which do not yet (a per-statement query timeout,
  error-text/recon hardening). Docs only — no behaviour or config change.

## [0.5.3] — 2026-07-31

### Added

- **Override seam on the tool-call audit log (`record_tool_call`).** An embedder that dispatches the
  tool handlers itself can now supply the values the log would otherwise infer from the model's own
  tool arguments and result body — `source`, `thread_id`, `correlation_id`, `user_question`,
  `org_id`, and the outcome (`success`, `row_count`, `error_kind`). Each of these override
  parameters defaults to `None` = "derive it the way you always have" (the recorded fact `raised` is
  a separate boolean, default `False`), so a caller that passes none of them gets byte-identical
  rows — the in-repo transport path is regression-pinned identical to before the seam existed. `_record_query`'s source is likewise
  readable from a context var, so the query log and the tool-call log can't disagree about what drove
  one execution.

### Changed

- **The audit row now fails toward honesty.** A caller with no result body to parse can now record a
  failure (previously the row defaulted to success — the one direction an audit log must never fail
  in); a raise outranks every override; naming an `error_kind` is itself a statement of failure; and
  a success never carries an error kind, so the row can't contradict itself.
- **Honest question provenance in the Activity drawer.** The `· self-reported` trust marker was
  stamped on every question unconditionally (the `source` column was never even selected). The column
  is now projected and a per-turn flag derived, so the marker is dropped only where the caller
  observed the question directly — and kept under any disagreement or uncertainty, because
  overstating trust is the one direction it must not fail.

## [0.5.2] — 2026-07-30

### Added

- **Per-request tool-visibility seam (`Adapters.tool_visibility`).** A consumer can now narrow the
  advertised MCP tool surface per caller via an optional `tool_visibility(tool_name) -> bool` on the
  adapters, applied in `build_server`. It is applied at *both* the list and the call seam — filtering
  only the list would leave an unlisted tool callable by name, a surface that looks narrowed while
  remaining open. A hidden tool answers as *absent* (the same `Unknown tool` a typo gets), not as
  refused, so the list can't be used as an oracle for what exists but is withheld. A predicate that
  raises hides the tool rather than granting it, and is logged rather than propagated, so one broken
  classification can't become an accidental grant or a dead transport for every other caller.
  Subtractive only — a surviving tool's description and input schema pass through untouched. `None`
  (the OSS default) is byte-identical to prior behaviour.

## [0.5.1] — 2026-07-30

### Security

- **Closed a read-only-guard bypass via a welded quoted identifier.** A double-quoted
  identifier is self-delimiting in SQL on **both** ends, so `SELECT*FROM"pg_read_file"(…)`
  and `SELECT "x"INTO evil FROM t` are valid statements with no whitespace either side of
  the quote. The guard's lexer dropped the quote characters without re-supplying those
  boundaries, fusing neighbouring tokens into one (`FROM"pg_class"` → `FROMpg_class`,
  `"x"INTO` → `xINTO`) and destroying the word-boundary anchor every deny-list pattern
  matches on — so the gate stopped *seeing* the token rather than allowing it, and returned
  no rejection. Verified against PostgreSQL 16: the leading form reads a server-side file
  and the trailing form creates a table from `SELECT … INTO`, both while the guard passed
  them. Row locks (`FOR SHARE`) were reachable the same way. The lexer now re-supplies a
  separator on either side, when and only when the quote was actually separating two word
  characters — so a qualified name (`t."current_user"`) still neutralizes to one token,
  `t.current_user`, rather than being split. This restores an invariant the lexer already
  documented for comments and literals ("never empty"); the identifier branch was the one
  place not honouring it. Prior corpus cases all happened to carry a space before the
  quote, which is why this stayed invisible; the corpus now pins both weld directions and
  asserts the neutralized token structure directly, so neither a one-sided fix nor a
  blanket separator can pass.

## [0.5.0] — 2026-07-25

### Changed

- **Renamed the per-datasource model files** so their names say what they are, now that a
  company-wide `organization.yaml` exists at the artifacts root. In each profile,
  `org.yaml` → `datasource.yaml` and `ORGANIZATION.md` → `datasource.md`. The model root's
  display-name field `organization:` is likewise `datasource:`, and the served per-datasource
  memory row uses `kind='datasource'`. The company record (`organization.yaml`, `org_id`,
  tenancy) is unchanged. No on-disk migration is provided — re-run `agami-connect` (or rename
  the two files by hand) for any pre-existing profile.

## [0.4.5] — 2026-07-15

Hosted/self-hosted server hardening: a real-wheel packaging fix, a multi-tenancy seam, and an
append-only instructions hook. **The local plugin path is behaviour-preserving** — everything
resolves to a single `local` org and existing deploys are byte-identical by default.

### Fixed

- **Migrations and static assets now ship inside the wheel.** In a real (non-editable) `pip install`,
  `store.MIGRATIONS_DIR` resolved outside the package, so the server could boot on an **empty schema**
  with no error, and the missing `static/` dir made app construction fail. Migrations moved into the
  package (`packages/agami-core/src/migrations/core`), both are packaged as package-data, and `run_migrations`
  now **raises** on a missing core-migration root instead of silently applying nothing. (Editable
  checkouts and the `pip install -e` Docker deploy were unaffected — which is why CI stayed green.)

### Added

- **Multi-tenancy: `org_id` scoping across serving, runtime logs, and credentials.** One deployment
  can host many tenants whose datasources collide on name (e.g. `prod`). `org_id` (default `local`) is
  threaded through every serving/runtime read+write; a redeploy DELETE is org-scoped (one tenant's
  reseed can't wipe another's same-named datasource); per-tenant credential env vars
  (`<ORG>_DATASOURCE_URL[__<PROFILE>]`) with a **fail-closed** rule (a named tenant never falls back to
  the org-less DSN); and a resolver may raise `PermissionError` to refuse a caller (clean 403, not 500).
  A plain OSS/self-host deploy is unchanged — everything resolves to `local`.
- **Append-only `extra_instructions` seam on the HTTP composition root.** `build_server(...)` /
  `create_app(...)` accept `extra_instructions` so a consumer can add to the model-facing MCP
  instructions without forking core. **Append-only, never replace** — so a consumer can't silently drop
  a safety directive (e.g. the sensitive-column output rule); no-op and byte-identical by default.

## [0.4.4] — 2026-07-14

Onboarding fix for the examples-validation (NL→SQL) dashboard.

### Fixed

- **Examples-validation dashboard: Edit/Add-note no longer fires on every card.** Each example's
  interaction state was keyed on its display number `n`, which `sm seed-validate` assigns **per
  subject area** (1..k) — so a dashboard combining multiple areas carried duplicate `n`, and clicking
  **Edit** (or **Add note**) on one card opened every card that shared that number. It also made the
  "Generate feedback" block ambiguous (`edit N` could match more than one example). The renderer now
  assigns a **stable global `1..N`** in render order — the single numbering shared by the interaction
  key, the `#N` label, the feedback block, and the apply lookup — and normalizes the items file to
  match, so `edit N` resolves unambiguously.

## [0.4.3] — 2026-07-14

Documentation-only release. No behavior changes; the executor and skills from 0.4.2 are unchanged.

### Changed

- **`agami-core` PyPI page is now a readable landing page.** Reframed
  `packages/agami-core/README.md` (the PyPI `long_description`) to lead with the value proposition
  and clarify the plugin-vs-`pip` audiences, and trimmed the deep HTTP-server internals down to a
  summary plus links to `deploy/README.md` and `docs/`. Added `[project.urls]`
  (Homepage/Repository/Documentation/Issues) so PyPI shows sidebar navigation. Publishing this
  version is what refreshes the live PyPI page.

## [0.4.2] — 2026-07-14

Onboarding hardening for the public launch — fixes to the first-run `/agami-connect` path — plus a
documentation pass. No breaking changes; the executor internals from 0.4.1 are unchanged.

### Fixed

- **Seed validation no longer rejects every seed example.** The zero-row validation probe wrapped each
  seed as `SELECT * FROM (<sql>) WHERE 1=0`; its own `SELECT *` tripped the `SELECT *` ban and every seed
  was rejected regardless of its SQL. The probe now projects `SELECT 1` — it still parses and plans the
  inner query, but no longer trips the ban.
- **DuckDB readiness now requires `pytz`.** DuckDB needs `pytz` to materialize `TIMESTAMP WITH TIME ZONE`
  values; the driver probe only checked `import duckdb`, so an interpreter missing `pytz` scored as ready
  and then failed at query time on any `timestamptz` column. `pytz` is now part of the DuckDB probe.
- **Approve operations auto-stamp their timestamp.** An approve op without a `signed_off_at` recorded
  `null` and the validator rejected the whole batch. The timestamp is now stamped at the CLI boundary
  (where the clock is available), so sign-off batches apply cleanly.

### Added

- **Headless sign-off (`sm approve-queue`).** A no-browser path that reads the pending review queue
  (Rule 1 + Rule 2), builds a self-stamped approve op per item, and applies it (`--kind` to narrow,
  `--dry-run` to preview) — so onboarding can complete without opening the review dashboard.
- **The no-DB sample clears its own pre-seed gate.** The sample's silent build auto-approves its pre-seed
  queue as `signer=system` before seeding; real databases keep the human sign-off gate.

### Docs

- **Launch positioning.** The self-hosted team server (`/agami-deploy`) is labeled **Early access (in
  testing)** throughout; the free-vs-paid copy leads with the value the hosted cloud adds.
- **README.** A **Databases supported** section (all engines + how each executes), VS Code/Cursor install
  clarified as Manage-Plugins-UI (not the CLI slash-command form), and the sample-query copy made generic.
- **Guides.** Onboarding docs (`duckdb pytz` install, explicit render flags, the headless sign-off path),
  a plain-English trust-layer intro, an `/agami-serve` (Claude Desktop) usage section, and an accurate
  `migrations/core` README (the self-hosted server schema).

## [0.4.1] — 2026-07-12

The self-hosted HTTP server now runs SQL **in-process** by default — no per-query subprocess fork,
no CSV round-trip — behind a swappable execution seam. Plus a correctness fix for Postgres/Redshift.

### Added

- **Executor seam (`ports.Executor`).** `execute_sql` is split into a shared, un-bypassable *guarded
  envelope* (read-only guard → semantic-model safety → resolve datasource → execute) and a swappable
  **`Executor` port**. The built-in executor is the default and behaviour is unchanged; a consumer can
  inject a custom executor (e.g. pooled / per-user-RBAC) via `create_app(adapters=…)` **behind the same
  guard** — one execution implementation, never forked.

### Changed

- **The HTTP server executes queries in-process by default.** Previously every query forked
  `python -m execute_sql`; now the served path runs through the executor seam in-process — no fork, no
  CSV serialize/re-parse round-trip, native rows. The local stdio path and the `python -m execute_sql`
  CLI still fork (the throwaway-process isolation is kept for the single-user tool). Successful query
  results are identical to before.
- **The per-call row cap is request-scoped.** `--max-rows` now rides a `ContextVar` (was a module
  global), so concurrent in-process queries with different caps can't affect each other.

### Fixed

- **Postgres/Redshift queries returned 0 rows through the Python executor.** A psycopg2 server-side
  (named) cursor reports `description = None` until the first fetch, and the result collector read it
  *before* fetching — so it concluded "no result set" and returned empty. It now fetches first, then
  reads the description. SQLite/MySQL/etc. (client-side cursors) were unaffected. (Present since the
  server-side cursor was introduced for bounded transfer.)

### Performance

- **Tool handler runs off the event loop.** The heavy query handler is offloaded via `run_blocking`
  (completing the async-offload work), so one slow query no longer stalls the server's event loop.

## [0.4.0] — 2026-07-12

Runtime scalability & safety hardening: the server stays responsive and bounded as model size,
result size, and concurrent load grow. Behaviour-preserving unless a note says otherwise.

### Added

- **Bounded result sets.** A query now materialises at most a row cap instead of the whole result:
  `AGAMI_SQL_MAX_ROWS` (default 1000) is the deployment cap; a per-call cap is available via the
  executor's `--max-rows` (which can only lower it). Truncation is flagged (`result.truncated`) so a
  cut-off result is never presented as complete. The SQL is never rewritten (no injected `LIMIT`);
  Postgres uses a server-side cursor so the cap bounds transfer, not just what's written.
- **Multi-worker HTTP server.** The server can run with `--workers=N` (uvicorn import-string
  factory). MCP session state is already stateless (JWT + Postgres), so it scales horizontally.

### Changed

- **OAuth refresh-token storage is now configurable — default `overwrite`.**
  `AGAMI_REFRESH_TOKEN_MODE` selects `overwrite` (default: each refresh UPDATEs the session's single
  token row in place — one row per session, no growing heap of dead tokens) or `rotate` (the prior
  behaviour: insert-new + revoke-old, keeping OAuth 2.1 **stolen-token reuse detection**, plus a
  cleanup that prunes only already-expired revoked rows). **Upgrade note:** the new default
  `overwrite` trades away reuse detection — a replayed stolen refresh token simply fails to
  authenticate instead of revoking the whole family. A deployment that wants family-revocation must
  set `AGAMI_REFRESH_TOKEN_MODE=rotate`. Also: used/expired one-time authorization codes are now
  cleared at authorize, and the query/activity logs (`query_executions` / `tool_calls`) are
  explicitly **retained** — never deleted by any default path — with a new `idx_query_executions_ts`
  index to keep newest-first reads fast as history grows.
- **Hosted safety guard is now fail-closed and DB-backed.** On the hosted server the fan/chasm-trap,
  table/column-scope, SELECT-\* and PII guards resolve the semantic model from the database (not only
  the `/artifacts` disk mount) and **refuse** a query when no model can be resolved — instead of
  silently running it unguarded. The local single-player path is unchanged (a not-yet-built model is
  still fine, not an error).

### Performance (behaviour-preserving)

- **Per-process semantic-model cache + single SQL parse.** The model loads once per process and the
  SQL is parsed once per query, with the safety-guard indices built once and shared — down from a
  reload/re-parse per query on a long-lived server. Biggest latency win.
- **Blocking work runs off the event loop.** Password hashing (argon2), OIDC HTTP calls, and the
  per-call audit write no longer stall the async server — one slow login can't freeze all traffic.
- **Incremental model-authoring validation.** Curation/enrichment re-validates only the edited area,
  and snapshots read each file once, so authoring a large (many-area) model no longer grows
  super-linearly. Same verdicts and snapshots.
- **Faster schema discovery.** `get_datasource_schema` resolves tables via an O(1) index instead of
  re-scanning the model per table — byte-identical output, faster on wide models.

## [0.3.9] — 2026-07-10

### Added

- **Composition seams for downstream extension (no-op by default).**
  `mcp_http.create_app(extra_tools={}, adapters=None)` lets a downstream consumer add MCP tools and
  inject the `ports.py` adapters without forking or monkeypatching core, and `tools.register(...)`
  adds a tool to the shared registry with a duplicate-name guard. An existing deploy is unaffected —
  `create_app()` with no arguments behaves exactly as before, and `execute_sql`'s schema is unchanged.
  (#100)
- **Migration-overlay seam.** The store can layer additional migration roots on top of core's, so a
  downstream package can ship its own migrations alongside agami-core's (empty/duplicate namespaces
  and non-directory roots are rejected). No change for a default install. (#101)

## [0.3.8] — 2026-07-06

### Added

- **OAuth refresh tokens — no more hourly re-login on the self-hosted server.** The token
  endpoint now issues a `refresh_token` and supports the `refresh_token` grant (RFC 6749 §6),
  so a connected client (claude.ai) silently renews the short-lived access token instead of
  redoing the full login every hour. Refresh tokens **rotate** on each use with **reuse
  detection** (replaying a rotated/stolen token revokes the whole family), are stored **hashed,
  never in plaintext**, and are revocable. Access tokens stay short-lived (1h). Both lifetimes are
  now env-configurable (`AGAMI_ACCESS_TOKEN_TTL` / `AGAMI_REFRESH_TOKEN_TTL`, seconds) with the
  same defaults when unset (access 1h, refresh 30-day idle). No action needed on upgrade — the
  new `oauth_refresh_token` table migrates in automatically on boot.

## [0.3.7] — 2026-07-06

### Added

- **Read-only database user guidance.** `/agami-connect` and `/agami-deploy` now
  recommend connecting agami with a **read-only** database user — agami only ever
  runs read-only SELECT queries, so read access is all it needs. A new
  [readonly-grants.md](plugins/agami/shared/readonly-grants.md) ships copy-paste
  `CREATE USER` / `GRANT SELECT` SQL for every supported dialect (Postgres/Redshift,
  MySQL, Snowflake, SQL Server, Oracle, Databricks, Trino, BigQuery). Ask agami for
  "the read-only grant" to get the exact SQL for your database.

### Changed

- **Self-host compose caps container log growth.** Every service now uses the
  `json-file` driver with `max-size: 10m` / `max-file: 3` (≤30 MB per container), so
  a long-running deploy on a small VM can't silently fill the disk — no VM-side
  `daemon.json` step needed. Also silenced a harmless `CLOUDFLARE_TUNNEL_TOKEN … not
  set` warning on non-tunnel deploys.

### Fixed

- **`list_datasources` no longer reports empty on a self-hosted server.** On a
  served deployment the warehouse/model is reached through the store, and the local
  `credentials` file never ships to the container — but `list_datasources` was the
  one tool still reading only that file, so it always returned "No profiles found …
  run agami-connect", even while `get_datasource_schema` and `execute_sql` worked
  against the deployed model. Because clients are told to call it first, they'd
  conclude nothing was connected. It now enumerates the served models from the store
  (the same seam every other tool already uses), and only falls back to the
  credentials file for the local plugin.

### Security

- **Hardened the read-only `execute_sql` gate.** SQL execution now runs through a
  single guard (`sql_guard`) at the shared executor, so the stdio server, the hosted
  HTTP server, the skills, and cron are all protected identically (previously the
  check lived only on the MCP tool path; a direct `python -m execute_sql` call — used
  by the skills and cron — was unguarded). Beyond "must start with `SELECT`/`WITH`",
  it now rejects multi-statement SQL (including bypasses hidden in string literals,
  comments, or double-quoted identifiers), data-modifying CTEs, transaction-control /
  session-state / prepared statements, `SELECT ... INTO`, row-level locks, and
  dangerous server-side functions (`pg_read_file`, `lo_export`, `dblink`,
  `copy_program`, `pg_sleep`, advisory locks, `query_to_xml`, …). Legitimate analytics
  SQL is unaffected — a large false-positive corpus pins that. Enforcement is not
  bypassable via `--no-safety` (that flag only skips the semantic-model pass).
- **Closed a dollar-quote statement-stacking bypass in that gate.** A `'` inside a
  Postgres/Snowflake/DuckDB `$$…$$` (or `$tag$…$tag$`) string desynced the literal
  stripper and could smuggle a second statement (`SELECT $$'$$ ; DROP TABLE x -- '`)
  past the multi-statement check. The gate now neutralizes comments and string /
  dollar literals in a single lexer-faithful pass (first-opened construct wins),
  refuses dialect-ambiguous MySQL comment forms (a bare `--x` and executable
  `/*! … */` comments), and also blocks sequence writes (`setval`/`nextval`) and
  server/replication control
  (`pg_stat_reset*`, `pg_switch_wal`, `pg_drop_replication_slot`, …). The guard module
  is also now packaged in the built wheel (it was missing from `py-modules`, which
  would have broken `import sql_guard` in an installed/containerized deploy).

## [0.3.6] — 2026-07-04

### Changed

- **`/agami-deploy` is easier to find and safe to re-run.** The config file is now
  a **visible `agami.env`** (not a hidden `.env`), and the skill opens it for you.
  A **re-run upgrades in place, non-destructively**: your typed password/secret and
  DSN are kept, any setting new in a version is surfaced (e.g. `DATASOURCE_URL`),
  and the image tag bumps only when you pass one — so a model update is just
  re-run + restart, and a version upgrade tells you exactly what's new.
- **Multi-datasource deploys are an explicit choice.** With more than one model,
  the skill asks which to deploy (all or a subset) and names the per-datasource
  `DATASOURCE_URL__<NAME>` to set; dropping one on a re-run removes it cleanly.

## [0.3.5] — 2026-07-04

### Fixed

- **Self-host deploy no longer crash-loops on artifact permissions.** The team
  server runs as a non-root container user; the deploy now stages the model
  **world-readable**, so the boot-time model load can't fail `Permission denied`
  on `datasource.md` under a mismatched host owner.
- **claude.ai connects to a self-hosted server.** The `/mcp` endpoint no longer
  answers the bare (no-trailing-slash) URL with a `307` redirect that the MCP
  client won't follow — the server normalizes it internally, so `{base}/mcp`
  works on every deploy profile (including the Caddy-less Cloud Run one).

### Changed

- **Warehouse credentials come from the environment (`DATASOURCE_URL`), not a
  mounted file.** The executor resolves a connection DSN from
  `DATASOURCE_URL[__<datasource>]` env-first, falling back to the local
  `credentials` file — one code path, no fork. The self-host bundle now carries
  the DSN in `.env` and **ships no secret**: `local/` (credentials, `.pgpass`)
  is never staged, and a re-run purges any stale copy from an older bundle.

## [0.3.4] — 2026-07-03

### Fixed

- **Table-prune step of a real-DB onboarding no longer crashes on an installed
  build.** The `discover` pass (which renders the prune page where you pick which
  tables to model) failed with `ModuleNotFoundError` on a pip/marketplace install;
  it now resolves its renderer via the plugin root and works everywhere.

### Docs

- Refreshed for the current release: README slimmed (self-hosting moved to
  `docs/self-hosting.md`), the published PyPI install surfaced
  (`pip install "agami-core[model]"`), and the changelog backfilled.

## [0.3.3] — 2026-07-02

### Fixed

- **Marketplace-install reliability.** Credential promotion and the Claude Desktop
  setup (`/agami-serve`) no longer fail on a fresh marketplace install — they resolve
  the bundled library the same way every other script does, and install the model
  engine through the single `sm install` path.
- **Externally-managed Python + package shadowing.** The installer now works on an
  externally-managed interpreter (Homebrew / PEP 668) and can no longer be shadowed by
  a partially-installed package (the model CLI is verified from a neutral path).

### Changed

- **Sample "watch it build" opens the model explorer** when the build completes, and
  skips the prompts that don't apply to the curated sample (no table-prune / org /
  data-dictionary questions).
- **First-time setup no longer shows a placeholder profile name** — it reads as
  "first-time setup" until you name your profile.

## [0.3.2] — 2026-07-01

### Added

- **Published to PyPI.** `pip install "agami-core[model]"` (and `[server]`) installs
  the library from the index, and the plugin's model-build step uses it automatically.
  Published via GitHub trusted publishing (no stored token). The self-host server image
  is published to GHCR (`ghcr.io/agamiai/agami-core`) so a deploy pulls it — no clone,
  no build.

## [0.3.1] — 2026-07-01

### Fixed

- **Marketplace installs can query and build models with no dev checkout.** Bundled the
  stdlib query library into the plugin, so a marketplace install answers questions with
  no `pip install`; and the model-build step installs the engine from a source that
  exists in a marketplace layout (the published package, else git) instead of a
  dev-only path.

## [0.3.0] — 2026-06-24

### Added

- **No-database sample (`/agami-connect sample`).** Ships *Acme Store*, a small
  local SQLite dataset (commerce + subscriptions) with a ready-made, signed-off
  semantic model. Goes from install to a governed, receipted answer in under a
  minute — no connection, no credentials, nothing leaving the machine. The
  bootstrap (Phase 0s) offers a fast copy-the-model path and a "watch it build
  live" rebuild path. Builds deterministically via the `sqlite3` CLI or a pure
  Python-stdlib fallback (no install required).
- **Model snapshots / `model_version`.** A model write now stamps a content-hashed
  snapshot under `<profile>/.snapshots/<hash>/`, so every answer's receipt pins a
  real `model_version` (previously `null` for all profiles) and old answers stay
  reproducible. New `sm snapshot <root>` CLI.
- **Deterministic interaction spine.** The mechanical parts of the skills are now
  scripts that emit a uniform `{ok, data, anomalies, needs_judgment}` contract, so
  the agent only makes judgment calls on genuine ambiguity:
  `connect_resolve.py` (one call resolves profile / credentials / interpreter +
  next-phase decision — fixes choosing a Python that can't connect),
  `parse_prune_block.py` (fixes a shell word-split that mangled table lists),
  `parse_model_feedback.py` (the dashboard back-channel), `csv_to_sections.py`
  (charts/tables get their numbers from the result CSV, not the model), and the
  `sm receipt` / `sm curate-gate` subcommands.

### Changed

- **Renamed LiteBi → agami-core.** The install identity is now `agami-core@agami`
  (marketplace `agami`, plugin `agami-core`); the version bump is breaking, so
  existing `agami@litebi` installs must re-add the marketplace to upgrade
  (`/plugin marketplace add AgamiAI/agami-core` → `/plugin install agami-core@agami`).
- **Relicensed Apache-2.0 → fair-code (the Agami Functional Use License / FUL).**
  Internal/team use stays free; exposing the data or the MCP to people outside your
  organization now requires a commercial license. See [LICENSE](LICENSE) and
  [LICENSING.md](LICENSING.md).
- **Repositioned around the trust layer.** README, marketplace, and plugin
  metadata now lead with the governance/trust stance ("the trust layer between AI
  and your data") instead of natural-language querying. Dropped the "BI" framing.
- **Quickstart leads with the sample** — the fastest path to a first governed
  answer, with the real-database flow following it.

### Security

- **Engine-level PII enforcement.** Raw projection of a column marked `sensitive`
  is refused in the shared executor (`runtime.check_sensitive_projection`, wired
  into `execute_sql.py`), so the same rule protects the Claude Code skill **and**
  the local MCP server. Aggregates, filters, joins, and `GROUP BY` over sensitive
  columns are still allowed — only raw per-row output is blocked.

## [0.2.2] — baseline

First version tracked in this changelog. Earlier history lives in the git log.

- The local-first **trust layer**: confidence + review state on every join,
  metric, and entity; single-reviewer sign-off; per-answer receipts (SQL, tables,
  relationships, metric definitions, freshness); a review dashboard.
- Schema introspection into a provider-portable, git-native YAML semantic model.
- NL→SQL generation and **local execution** across Postgres, Supabase, Redshift,
  MySQL, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino, DuckDB, and
  SQLite.
- Corrections with attribution, persisted to an `examples.yaml` few-shot library.
- An optional local **MCP server** (`agami serve`) for use from Claude Desktop and
  other clients — stdio, no auth, no network.
- Fan-trap / chasm-trap pre-flight that refuses to silently double-count.

[0.3.9]: https://github.com/AgamiAI/agami-core/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/AgamiAI/agami-core/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/AgamiAI/agami-core/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/AgamiAI/agami-core/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/AgamiAI/agami-core/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/AgamiAI/agami-core/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/AgamiAI/agami-core/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/AgamiAI/agami-core/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/AgamiAI/agami-core/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/AgamiAI/agami-core/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/AgamiAI/agami-core/releases/tag/v0.2.2
