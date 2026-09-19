# RSS integration recovery

## Outcomes

- Replace the reader without losing the current subscription list or retiring the old reader before parity is measured.
- Keep the existing critic (rubric, deduplication, verdict log and explanations). A tag-only classifier is not a replacement.
- Support explicit provider protocols without claiming credentials, quotas or model compatibility that have not been tested.
- Keep the critic off public ingress. Notifications require authentication; no test sends real email.
- Fail visibly on source/authentication/provider errors. Dry runs must not mutate reader state or the verdict log.

## Acceptance

Unit tests and lint pass; an isolated real reader accepts subscriptions and yields normalized entries; starring twice remains starred; unauthorized critic requests fail; protocol contract tests cover supported APIs. Production cutover, shared proxy changes and workflow activation require a separate deployment gate. Existing production services remain unchanged during validation.
