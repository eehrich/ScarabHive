# forge — GitLab und GitHub für den Coder

Stand 28.09.2026. Auftrag des Nutzers: Der `coder` soll auf GitLab zugreifen —
Tickets abarbeiten, Merge Requests prüfen und bearbeiten, Builds kontrollieren.
GitHub muss ebenfalls gehen, als optionaler zweiter Anschluss.

## 1. Entscheidungen

| # | Entscheidung | Begründung |
|---|---|---|
| E1 | **Eigenes Plugin, kein MCP.** | Die GitLab-MCPs (GitLabs eingebautes `/api/v4/mcp`, `@zereight/mcp-gitlab`) reichen nur die REST-API durch — anders als bei n8n steckt darin keine Logik, die wir nicht nachbauen wollen. Ein eigenes Plugin deckelt Ergebnisse (Job-Logs, Diffs haben schnell Megabytes), kennzeichnet fremden Text, prüft vor dem Merge und schreibt unsere Status-Zeilen. Das offizielle MCP braucht außerdem Premium/Ultimate und OAuth im Browser. |
| E2 | **Ein Tool-Satz, zwei Backends.** GitLab zuerst, GitHub optional. | Der Coder lernt einen Ablauf (Skill `forge-workflow`), nicht zwei fremde Tool-Sätze. Welches Backend, entscheidet die Konfiguration des Repos — nie das Modell. |
| E3 | **Self-hosted, konfigurierbar** (Nutzer 28.09.). | `api_url` pro Host, eigene CA über `ca_bundle`. GitHub Enterprise Server geht über dieselbe Einstellung. |
| E4 | **Der Coder darf mergen** (Nutzer 28.09.). | `pr_merge` prüft selbst: offen, kein Draft, CI auf dem aktuellen Kopf-Commit grün, keine offenen Threads. Der geprüfte Commit geht als `sha` an die Merge-API — kommt dazwischen ein Push, lehnt die Plattform ab. Freigabe pro Repo (`allow_merge`), im Code standardmäßig aus. |
| E5 | **Gemeint ist unser `coder`, der `coding_cli` nutzt** (Nutzer 28.09.). | Der `coder` bekommt die forge-Tools und steuert den Ablauf. Claude Code bekommt nichts davon; es programmiert weiter nur im Worktree. |
| E6 | **Gepusht wird nur über forge.** | `coder_shell` sperrt `git push` ([tools.yaml](../../coder/agents/tools.yaml)), `coding_cli` pusht nie. `forge_push` pusht nur auf Branches mit dem Präfix (`branch_prefix`, Standard `scarabhive/`), nie mit Force. Das Token erreicht git nur als Credential für den Origin der Plattform (ein Helper liest es aus der Umgebung des einen git-Prozesses; ein `extraHeader` scheiterte an git-lfs, facts.md F-GL10) — nicht in der Remote-URL, nicht in der Konfiguration des Klons, nicht auf einer Kommandozeile, und kein Hook läuft damit. Zertifikate werden immer geprüft, auch gegen eine globale `http.sslVerify false` (F-ENV1). Geheim vor der Shell des Coders ist es nicht: die kann `config/secrets.env` lesen wie jede Datei. Die bindende Grenze ist E7. |
| E7 | **Die Rechte regelt die Plattform.** | Bot-Konto mit Rolle *Developer*. Default-Branch geschützt: Push nur Maintainer (oder niemand), Merge Developer + Maintainer. Dieser Riegel hält auch, wenn forge einen Fehler hat. |
| E8 | **Fremder Text ist Daten.** | Issue-, Kommentar- und Log-Text schreiben Menschen und Programme. forge gibt ihn als `{"untrusted": true, "content": …}` zurück — wie n8n und `coding_cli`. Ein Ticket bearbeitet der Coder nur, wenn es dem Bot-Konto zugewiesen ist oder der Nutzer es ihm nennt; zuweisen kann er sich nicht selbst (kein Tool dafür). Gemergt wird nur auf Wunsch des Nutzers, nie weil Text auf der Plattform es verlangt (Review 28.09., S3). |
| E9 | **CI-Warten blockiert, kein Weckruf.** | `ci_status` mit `wait_s` (≤ 600 s) — derselbe Weg wie `coding_cli_get_run`. Direkt nach einem Push gibt es noch keine Pipeline (F-CI1); „keine" wird deshalb bis 45 s abgewartet, danach als „keine CI" genommen. Der Weckruf-Mechanismus aus n8n (Watch, Marken über Prozesse) wäre der nächste Schritt, falls Pipelines regelmäßig länger laufen. |
| E10 | **Webhooks: GitLab weckt den Coder** (Nutzer 28.09.: „Webhooks brauchen wir"). | Ein zugewiesenes Issue startet einen Coder-Lauf, ein Kommentar oder eine rote Pipeline weckt die Session, die daran arbeitet. Aufbau in §7. GitHub nicht: github.com erreicht eine ScarabHive im LAN nicht; dieselbe Verteilung nähme es auf. |

## 2. Aufbau

```
src/plugins/forge/
  plugin.toml, plugin.py
  server.py      Tools: Argumente prüfen, Repo auflösen, Policy, Deckel, Status
  http.py        gemeinsamer httpx-Client, Fehlerklassen, Paging
  gitlab.py      REST v4 → einheitliche Form
  github.py      REST + GraphQL (Review-Threads) → einheitliche Form
  gitops.py      clone/fetch/switch/push mit Token nur in der Umgebung
  schema.yaml    Tools (Jinja: nur angeboten, wenn ein Repo konfiguriert ist)
  agents/forge.yaml            Server-Eintrag, enabled, ohne Repos
  skills/forge-workflow/SKILL.md   Ablauf Ticket → Merge
  tests/         Unit (MockTransport, lokale Bare-Repos), Konfig, Live
  tests/live/gitlab/compose.yaml   Testinstanz GitLab CE + Runner
```

**Einheitliche Form.** Backends geben normalisierte Dicts zurück; `server.py`
kennt keine Plattform. CI-Zustände werden auf `pending | running | success |
failed | canceled | skipped | manual` abgebildet, Merge-/Pull-Requests heißen
im Tool-Namen `pr`.

**Wo sich die Plattformen unterscheiden** (die eigentliche Arbeit):

- *Kommentare:* GitLab hat Diskussionen, per REST auflösbar. GitHub hat
  Gesprächs-Kommentare, Inline-Review-Kommentare und Reviews; Threads löst
  nur GraphQL auf. `pr_discussions` führt sie zu einer Liste zusammen.
- *CI:* GitLab — Pipeline pro Ref. GitHub — Check-Runs (Actions und fremde CI)
  plus alte Commit-Statuses auf dem Kopf-Commit; der Zustand ist die Summe.
- *Logs:* GitLab `/jobs/:id/trace` (Text). GitHub `/actions/jobs/:id/logs`
  (Weiterleitung auf Text); ein Check-Run einer fremden CI hat kein Log hier.

## 3. Konfiguration

Der Server-Eintrag kommt mit dem Plugin (`agents/forge.yaml`, ohne Repos).
Hosts und Repos sind Werte des Betreibers und gehören in `config/plugins.yaml`
(die Einträge werden zusammengeführt):

```yaml
plugins:
  servers:
    forge:
      hosts:
        git.firma.de:
          provider: gitlab
          api_url: https://git.firma.de/api/v4
          token_env: FORGE_GITLAB_TOKEN      # Name der Variable in config/secrets.env
          ca_bundle: ""                      # eigene CA, falls nötig
      repos:
        app:                                 # der Name, den der Coder benutzt
          host: git.firma.de
          project: team/app
          allow_merge: true
          # path: data/workspace/forge/app   # lokaler Klon (Standard)
      branch_prefix: "scarabhive/"
      merge_method: squash                   # merge | squash | rebase
```

Ohne Repo bietet die Instanz keine Tools an. Der Token steht nie in der YAML,
nur der Name der Variable.

## 4. Tools

| Tool | Tut | Policy |
|---|---|---|
| `checkout` | klont bzw. holt den Stand, legt einen Arbeits-Branch an oder wechselt | nur in den konfigurierten Pfad; nie über ungesicherte Änderungen hinweg |
| `push` | pusht einen lokalen Branch | Ziel-Branch mit Präfix, nie Default, nie Force |
| `issue_list`, `issue_get` | Tickets lesen | Text untrusted, gedeckelt |
| `issue_comment`, `issue_update` | kommentieren, Labels, schließen/öffnen | zuweisen bleibt Menschen |
| `pr_list`, `pr_get`, `pr_diff`, `pr_discussions` | Merge Requests lesen | Diff seitenweise pro Datei |
| `pr_create`, `pr_update` | anlegen (optional „Closes #n"), Titel/Text/Draft | Quelle muss gepusht sein |
| `pr_comment` | kommentieren, auf Thread antworten, Thread auflösen, Zeilenkommentar | |
| `pr_merge` | mergen | `allow_merge`, `sha` Pflicht, grün auf dem Kopf-Commit, keine offenen Threads oder Änderungswünsche; danach nur Branches unter dem Präfix löschen |
| `ci_status` | Pipeline/Checks einer PR, eines Branches oder Commits; wartet optional | |
| `ci_job_log` | Ende des Job-Logs, optional gefiltert | ANSI entfernt, gedeckelt, untrusted |
| `ci_retry` | einen Job neu starten | |

## 5. Plattform einrichten

**GitLab:** Bot-Nutzer (oder Project/Group Access Token) mit Rolle
*Developer*, Personal Access Token mit Scope `api`. Default-Branch geschützt:
*Allowed to push* = Maintainers oder No one, *Allowed to merge* = Developers +
Maintainers. Wer „Pipelines must succeed" einschaltet, hat den Riegel doppelt.

**GitHub:** Fine-grained Token nur für die freigegebenen Repos — Contents,
Issues, Pull requests, Actions: read/write; Checks, Commit statuses, Metadata:
read. Ruleset oder Branch-Protection auf dem Default-Branch.

## 6. Messen

Live-Testinstanz: GitLab CE 19.4.1 mit Runner auf dem Docker-Testhost
(`tests/live/gitlab/`). Live-Tests laufen nur mit `FORGE_LIVE=1`. Befunde
stehen in `facts.md`.

## 7. Webhooks

```
GitLab ──POST /plugins/forge/webhook──▶ API ──▶ events.db ──notify──▶ Session
          X-Gitlab-Token                 │        (Eingang)      (Weckruf: frei → neuer
                                         │                         agent-cli-Prozess,
                                         └─ neue Arbeit: Session    belegt → nächster Schritt)
                                            für webhook.user anlegen
Hook deliver_webhook_events (pre_llm_call) ──▶ übergibt den Eingang als eine Nachricht
```

| # | Entscheidung | Warum |
|---|---|---|
| W1 | **Route auf der Haupt-API, nicht eigener Port.** | Nur der API-Prozess hängt Web-Router ein; ein Listener im Plugin liefe in jedem `agent-cli`-Prozess mit. Wie Stategraphs Callback: der Pfad wird in beiden Auth-Schichten anonym freigegeben, die Route prüft selbst. Die API muss dafür im LAN lauschen (`network.host`) — und darf dem LAN dann nur diesen Pfad zeigen: `network.remote_paths` (Kern, `auth/remote_paths.py`) beantwortet jede andere Anfrage, die nicht von diesem Rechner kommt, mit 404, vor jeder anderen Schicht. Ohne diese Wache hätte jeder Rechner im LAN sich registrieren (`POST /auth/register` ist offen) und den Coder mit Shell starten können (Review des Webhooks). |
| W2 | **Echtheit über `X-Gitlab-Token`**, konstant-zeitig gegen `webhook_secret_env` des Hosts. | GitLab signiert nicht, es schickt das Geheimnis mit. Ohne konfiguriertes Geheimnis nimmt die Route nichts an. Körper höchstens 1 MB, nur konfigurierte Projekte. |
| W3 | **Antwort sofort, Arbeit danach.** | GitLab wartet 10 s und schaltet einen Hook nach Fehlern ab. Doppelte Zustellung erkennt der `Idempotency-Key` — ein erneutes Senden behält ihn, die `X-Gitlab-Event-UUID` erneuert es (F-GL15). Scheitert die Verteilung, wird der Schlüssel wieder vergessen, damit ein erneutes Senden durchkommt. |
| W4 | **Eine Verteilung für alles: Eingang + Weckruf.** | Jedes Ereignis wird ein Eintrag im Eingang einer Session, dann `notify`. Freie Session → neuer Prozess setzt sie fort; belegte → Marke, der Hook übergibt beim nächsten Schritt. Neue Arbeit bekommt eine neue Session (Agent `coder`, Nutzer `webhook.user`) und läuft genauso an — kein zweiter Startweg. |
| W5 | **Welche Session woran arbeitet, merkt sich forge.** | `checkout` (Branch unter dem Präfix), `pr_create` (MR, Branch, geschlossenes Issue), `pr_comment` (MR) und `issue_comment` binden ihr Ziel an die aufrufende Session (`_session_id`, `_user_id`); die Verteilung bindet das Issue einer neuen Session. Die jüngste Bindung gilt. Eine Bindung an eine gelöschte Session fällt beim nächsten Ereignis weg; neue Arbeit bekommt dann eine neue Session. Pro Ziel verteilt forge eins nach dem anderen: Zuweisung und Erwähnung zugleich starten eine Session, nicht zwei. |
| W6 | **Ereignisse:** Issue dem Bot zugewiesen → neue Arbeit (oder die gebundene Session); Kommentar auf gebundenem MR/Issue → wecken; Kommentar, der den Bot erwähnt, ohne Bindung → neue Session, die antwortet (kein Auftrag zum Committen); Pipeline rot auf gebundenem Branch → wecken. Issue-Änderungen und Kommentare des Bots selbst werden verworfen (sonst weckt er sich mit jedem Kommentar) — eine Pipeline nicht: ihr `user` ist, wer gepusht hat, auf seinen Branches also der Bot. Eine Erwähnung auf einem gebundenen Ziel ist für dessen Session ein Kommentar wie jeder andere. | Das sind die Stellen, an denen sonst ein Mensch den Coder anstoßen müsste. |
| W7 | **Die Nachricht nennt nur Kennungen**, keinen fremden Text: „Neuer Kommentar von @alice auf !14 (Notiz 345) — lies ihn mit forge_pr_discussions". | Der Eingang wird als vertrauenswürdige Nachricht übergeben; Titel und Kommentare kommen weiter über die Tools, als `untrusted`. |
| W8 | **Gemergt wird aus einem Webhook-Lauf nur mit `webhook.merge: true`.** Standard: MR öffnen, CI grün, dann berichten — ein Mensch mergt. | Niemand hat in dieser Session um den Merge gebeten (E8). |
| W9 | **Deckel:** höchstens `webhook.max_new_per_hour` neue Sessions (Standard 6) und `webhook.max_wakes_per_hour` Weckrufe (Standard 20). Über dem Weck-Deckel bleibt die Nachricht im Eingang und kommt mit dem nächsten Lauf; über dem Deckel für neue Sessions wird nichts gestartet, und forge vergisst den `Idempotency-Key` — ein erneutes Senden aus GitLab kommt später durch. | Tokens kosten Geld; ein Skript, das Issues zuweist, oder jeder Kommentator eines öffentlichen Projekts soll keine Rechnung erzeugen. Der Weckruf selbst verhindert nur gleichzeitige Läufe, keine nacheinander (Review des Webhooks). Ohne `session_presence` startet forge keine Session — nichts würde sie laufen lassen. |
