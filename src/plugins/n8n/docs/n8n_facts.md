# n8n-Fakten: Test-Instanz n8n 2.39.9 (Community, Docker, SQLite)

Stand: 21.09.2026, nach dem Umbau auf den Instanz-MCP und Review-Runde 2. Dieses Dokument ist die Grundlage, auf die sich `docs/design.md` stützt. **Vor dem Bau prüfen.** Beispiel-URLs verwenden `http://localhost:5678`; `<base>` steht für die Basis-URL der Instanz.

**Kennzeichnung der Fakten**
- **[gemessen]**: Ein Explorer hat es ausgeführt und das Ergebnis gesehen.
- **[dokumentiert]**: Spec, Quelltext oder Doku; die Quelle steht dabei.
- **[angenommen]**: ungeprüft.
- **ÜBERHOLT**: Die Aussage stimmt nicht mehr oder trägt das Design nicht mehr. Der Ersatz ist genannt, falls es einen gibt; die ID bleibt stehen, damit alte Verweise auflösbar sind.
- **nicht mehr tragend**: Die Aussage stimmt weiterhin, aber seit dem Umbau auf den Instanz-MCP (design §2) baut kein Designteil mehr darauf. Sie steht unter „verworfen, weil“ (design §13).

**Quellen**
- Probe-Skripte und Rohdaten liegen im Scratchpad der Messläufe:
  - `work/p1.py`–`p20.py`: API-Karte,
  - `work/nt/`: Nodes-Karte,
  - `work/connect/`: Bridge-Karte,
  - `work/probe_*.py`: unsere Seite,
  - `work/rv/m1.py`–`m3.py`, `rv_lic.py`: Review-Runde 1,
  - `mcp_probe.py`, `validate_cases.py`: Instanz-MCP (Hauptsession),
  - `work2/t1*.py`–`t9b.py`: Instanz-MCP-Messung,
  - `work2/r1_*`–`r6.py`: Review-Runde 2,
  - `smoke.py`, `m7_m9.py`, `newcred.py`: Bau Phase 1a,
  - `review/*.py`: Review-Runde 3 (großes Review), `fix/*.py`: Nachmessungen dazu.
- `openapi.yml` ist die Spec der Instanz (info.version 1.1.1).
- `work/paths.txt` ist die `/discover`-Ausgabe (Endpunkt → Scope).
- Die Markierungen:
  - „(Nachmessung)“: nachgeprüft am 21.09.,
  - „(Review)“: im Review gemessen und danach erneut ausgeführt,
  - „(Review 2)“: in Review-Runde 2 gemessen,
  - „(Hauptsession)“: von der Hauptsession gemessen.
- Kein Key und kein Passwort ist in Skriptausgaben oder Dateien gelandet.

## Instanz
- **F-INST1 [gemessen]** n8n 2.39.9 mit Community-Lizenz und SQLite. `executionMode: regular`, die Public API ist aktiv. Quelle: `/rest/settings` `versionCli`, `/rest/license` planName Community.
- **F-INST2 [gemessen]** `/healthz` und `/healthz/readiness` liefern 200. `/metrics` liefert 404 [angenommen: Grund ist `N8N_METRICS`].
- **F-INST3 [gemessen, Nachmessung]** Stand nach allen Messläufen: 0 Workflows, 0 Tags, 0 `zz-probe-*`. Quellen:
  - `GET /api/v1/workflows?limit=250`, `/tags` und `/credentials`,
  - die Aufräumschritte von t1–t7 [M-MCP-28], t8/t9 [M-MCP-37] und r2–r6 [M-MCP-45].
- **F-INST4 [gemessen, Review]** Instanz-Einstellungen:
  - `excludeNodes` = executeCommand, localFileTrigger, e2eTest, dynamicCredentialCheck.
  - `security.blockFileAccessToN8nFiles: true`. Laut Name schützt das nur das n8n-Verzeichnis, nicht das übrige Dateisystem [dokumentiert: Setting-Name; die Reichweite ist angenommen].

  Quelle: `/rest/settings` (rv_lic.py, settings_auth.json).

## Deploy
- **F-DEP1 [gemessen, Nachmessung]** In `src/plugins/n8n/docs/deploy/docker-compose.yml` steht:
  - Zeile 7: `restart: unless-stopped`.
  - Zeile 15: `N8N_SECURE_COOKIE` mit Default `true`.
  - Zeile 18: `N8N_RUNNERS_ENABLED: "true"` (Task-Runner für Code-Knoten; siehe [M-MCP-38]).
  - Zeilen 21–22: `N8N_MCP_MANAGED_BY_ENV: "true"` und `N8N_MCP_ACCESS_ENABLED: "true"` [M-MCP-22].

  Auf der Test-Instanz setzt die `.env` `N8N_SECURE_COOKIE=false`, weil sie per HTTP über das Netz erreicht wird; daher das Cookie ohne `Secure` [F-EMB1].
- **F-DEP2 ÜBERHOLT (entfällt)**
- **F-DEP3 ÜBERHOLT (entfällt)** Gemessen bleibt nur: Die Compose-Datei setzt kein Speicherlimit.
- **F-DEP4 ÜBERHOLT → F-DEP5** `.env` und `CREDENTIALS` waren nicht git-ignored.
- **F-DEP5 [gemessen, 21.09.]** `src/plugins/n8n/docs/deploy/.gitignore` enthält `.env` und `CREDENTIALS`. `git check-ignore -v` liefert für beide rc=0 und eine Treffer-Zeile aus dieser Datei.
- **F-DEP6 [gemessen, Hauptsession]** Stand der Deploy-Dateien (Phase 0 aus design §11 umgesetzt):
  - `docs/deploy/` enthält `docker-compose.yml` (Image `docker.n8n.io/n8nio/n8n:2.39.9`, ohne Kommentare, mit den MCP-Variablen [F-DEP1]), `.env.example`, `setup_owner.sh`, `.gitignore` und `README.md`.
  - `.env.example` enthält `N8N_ENCRYPTION_KEY`, `N8N_PUBLIC_URL`, `N8N_PORT`, `GENERIC_TIMEZONE` und `N8N_SECURE_COOKIE`.
  - `setup_owner.sh` führt jeden Schritt einzeln und wiederholbar aus: Owner anlegen (400, wenn es ihn schon gibt), einloggen, Public-API-Key mit den **vier lesenden Scopes** `workflow:read`, `workflow:list`, `execution:read`, `execution:list` anlegen, prüfen, dass der Instanz-MCP an ist (sonst Abbruch mit Hinweis auf die Compose-Variablen), MCP-Key per `POST /rest/mcp/api-key/rotate` holen. Jeder Schritt entfällt, wenn sein Wert schon in `CREDENTIALS` steht. Keys werden nie ausgegeben.
  - Auf der Test-Instanz ausgeführt: Owner vorhanden, Key angelegt, MCP an, MCP-Key rotiert; der alte Voll-Key [F-AUTH4] ist gelöscht.
- **F-DEP7 [gemessen, Hauptsession]** Ohne `N8N_HOST`, `N8N_PORT` und `N8N_PROTOCOL` leitet n8n seine URLs aus `N8N_EDITOR_BASE_URL` und `WEBHOOK_URL` ab: `/rest/settings` zeigt `urlBaseWebhook` und `urlBaseEditor` gleich `N8N_PUBLIC_URL`. Owner-Login, API-Key und MCP-Key überlebten das Neuanlegen des Containers mit der generischen Compose-Datei, weil `N8N_ENCRYPTION_KEY` gleich blieb.

## Auth (Public API)
- **F-AUTH1 [gemessen]** Die Public API nimmt den Key im Header `X-N8N-API-KEY`. Bearer mit dem API-Key ergibt 401. Quelle: p3.py.
- **F-AUTH2 [gemessen]** Ein fehlender oder falscher Key ergibt 401 `{"message":"Unauthorized"}`. Auch nach 100 Fehlversuchen gibt es keine Sperre. Quelle: p15.py.
- **F-AUTH3 [gemessen]** Scoped Keys funktionieren. Fehlt ein Scope, kommt 403 `{"message":"Forbidden"}` ohne `reason`. `/discover` zeigt nur die erlaubten Endpunkte. Quelle: p16.py, mit einem temporären Key, der danach gelöscht wurde.
- **F-AUTH4 ÜBERHOLT → F-AUTH8** Der erste Key der Test-Instanz hatte alle 105 Owner-Scopes, darunter `user:*`, LDAP, SAML und `communityPackage:install`, weil die erste Fassung von `setup_owner.sh` alle Scopes anforderte. Das Skript ist korrigiert [F-DEP6], der Key gelöscht. Quelle: p2.py `/discover`, `/rest/api-keys/scopes`.
- **F-AUTH5 [gemessen]** Die Public API hat kein Rate-Limit: 300 GET mit 10 Threads ergaben nur 200er, es gibt keine Ratelimit-Header, und die Spec kennt kein 429. Quelle: p15.py, grep in openapi.yml. Das Gegenstück ist der Instanz-MCP [M-MCP-24].
- **F-AUTH6 [dokumentiert]** Scope je Endpunkt (Auszug):

  | Endpunkt | Scope |
  |---|---|
  | `GET /workflows` | `workflow:list` |
  | `GET /workflows/{id}` | `workflow:read` |
  | `PUT /workflows/{id}` | `workflow:update` |
  | `POST /workflows/{id}/publish` und `/activate` | `workflow:activate` |
  | `/unpublish` und `/deactivate` | `workflow:deactivate` |
  | **`POST /workflows/{id}/archive` und `/unarchive`** | **`workflow:delete`** |
  | `GET /executions` | `execution:list` |
  | `GET /executions/{id}` | `execution:read` |
  | `GET /workflows/{id}/tags` | `workflowTags:list` |
  | `POST /tags` | `tag:create` |

  Quelle: `work/paths.txt` (`/discover`).
- **F-AUTH7 [dokumentiert]** Weitere Details aus der Spec:
  - `GET /workflows/{id}` liefert im Schema u. a. `tags`, `isArchived`, `versionId`, `activeVersionId`, `activeVersion`, `settings` und `nodes`.
  - `GET /workflows` kennt die Query-Parameter `tags`, `name`, `active`, `projectId`, `limit`, `cursor` und `excludePinnedData`.
  - `GET /executions` kennt `workflowId`, `status`, `startedAfter`, `startedBefore`, `includeData`, `limit` und `cursor`.

  Quelle: openapi.yml, per yaml.safe_load am 21.09.
- **F-AUTH8 [gemessen, Hauptsession]** Der Key mit den vier lesenden Scopes [F-DEP6]:
  - `GET /workflows` und `GET /executions` liefern 200; `POST /workflows` und `GET /users` liefern 403.
  - `GET /workflows/{id}` enthält `tags`, `isArchived` und `settings.availableInMCP`; `GET /workflows?tags=<name>` filtert richtig (beantwortet M12). Ein weiterer Scope ist für den Riegel nicht nötig.

  Quelle: m12.py (Workflow per MCP angelegt und getaggt, mit dem Lese-Key gelesen, danach archiviert und gelöscht; 0 übrig).

## Public API allgemein
- **F-API1 [dokumentiert]** Die Spec hat 134 Operationen. Keine davon:
  - führt einen Workflow aus,
  - führt einen einzelnen Knoten aus,
  - validiert, ohne zu speichern.

  Etwas ausführen lässt sich nur über:
  - den Produktions-Webhook,
  - `POST /executions/{id}/retry`,
  - test-runs (lizenzgesperrt).

  Quelle: openapi.yml, grep `x-eov-operation-id`. Der Instanz-MCP füllt die Lücke [M-MCP-3].
- **F-API2 [gemessen]** Körper von Schreib-Requests:
  - Pflichtfelder sind `name`, `nodes`, `connections` und `settings`; `settings: {}` reicht.
  - Read-only-Felder (`id`, `active`, `versionId` …) und unbekannte Keys ergeben 400.
  - POST liefert 200, nicht 201.

  Quelle: p5.py.
- **F-API3 [gemessen]** Paginiert wird per Cursor: `{data, nextCursor}`. Der Cursor ist base64-kodiertes JSON `{lastId, limit}`; ein ungültiger Cursor ergibt 400. Quelle: p12.py.
- **F-API4 [angenommen]** Ob `limit>250` gekappt wird, ist ungeprüft. Gemessen ist nur, dass 300 und 999 kein 400 liefern.

## PUT und Versionen (Public API) — nicht mehr tragend
Das Plugin schreibt nicht mehr über die Public API (design §2, §13). Die folgenden Fakten gelten weiter; sie werden wichtig, falls jemand doch per PUT schreibt.
- **F-PUT1 [gemessen]** Ein GET-Body lässt sich nicht unverändert per PUT zurückschreiben, denn `id` und `versionId` sind read-only (400). Quelle: p8.py.
- **F-PUT2 [gemessen]** Bei `description:null` (so liefert GET es) antwortet PUT mit 400. `staticData:null` und `pinData:null` werden angenommen. Schreibbar sind:
  - `name, nodes, connections, settings, staticData, pinData, nodeGroups`,
  - `description` (nur bei PUT),
  - bei POST zusätzlich `projectId` und `parentFolderId`.

  Quelle: p8.py; openapi.yml.
- **F-PUT3 [gemessen]** Es gibt kein Optimistic Locking: `versionId` im Body ergibt 400 (read-only), und die Spec kennt kein If-Match. Quelle: p8.py.
- **F-PUT4 [gemessen]** PUT mit kaputtem Inhalt auf einen **aktiven** Workflow (Default `publishIfActive=true`) liefert 400. Der Entwurf wird aber **trotzdem gespeichert** und bekommt eine neue versionId; die alte Version bleibt live. Quelle: p14.py.
- **F-PUT5 [gemessen]** Die beiden Versionsfelder:
  - `versionId` ist der Entwurf, `activeVersionId` der Live-Stand.
  - `PUT ?publishIfActive=false` speichert nur einen Entwurf.
  - Ein Standard-PUT veröffentlicht sofort.
  - Ein identischer PUT erzeugt keine neue Version.
  - `publish {versionId}` rollt zurück; eine unbekannte Version ergibt 404.

  Quelle: p9.py.
- **F-PUT6 [angenommen]** Wie sich `publishIfActive=false` mit kaputtem Inhalt auf einem aktiven Workflow verhält, ist ungemessen.
- **F-PUT7 [dokumentiert]** Laut Spec hat die automatische Neuveröffentlichung per PUT zwei Folgen:
  - Sie braucht zusätzlich `workflow:activate`.
  - Fehlt der Scope, wird der Stand trotzdem als Entwurf gespeichert, mit 403 `reason: insufficient_api_key_scope`.

  Quelle: `work/wf_ops.txt` (PUT-Beschreibung).

## Validierung durch den Server (Public API)
- **F-VAL1 [gemessen]** Beim Anlegen oder bei PUT lehnt der Server Strukturfehler ab: 400 „Workflow structure is invalid“. Das betrifft:
  - `unknown_connection_target/source`,
  - `duplicate_node_name`,
  - fehlendes type, name oder position,
  - falsche Form der Connections.

  Quelle: p13.py.
- **F-VAL2 [gemessen]** Folgendes wird beim Anlegen still gespeichert und **erst beim Publish** mit 400 abgelehnt:
  - ein unbekannter Knotentyp,
  - eine falsche typeVersion bei Nicht-Triggern,
  - ein fehlender Pflichtparameter,
  - eine fehlende Pflicht-Credential (`Missing required credential: slackApi`),
  - ein fehlender Trigger.

  Quelle: p13.py, nt/p5-p6.
- **F-VAL3 [gemessen]** Folgendes kommt durch **alle** Prüfungen der Public API und scheitert erst zur Laufzeit oder still:
  - Webhook-typeVersion 99 bzw. 9.9,
  - leerer Webhook-Pfad (danach 404),
  - `httpMethod:"FROB"`,
  - `method:"FOO"`,
  - ein unbekannter Parameter,
  - eine nicht existierende Credential-ID,
  - `responseMode:responseNode` ohne Respond-Knoten (Laufzeit-500),
  - AI Agent ohne Sprachmodell,
  - ein unvollständiger Ausdruck `={{ …` (läuft).

  Quelle: p13.py, nt/p5-p6. Wie die Validatoren des Instanz-MCP damit umgehen, steht in [M-MCP-H8][M-MCP-H9][M-MCP-8]–[M-MCP-11].
- **F-VAL4 [gemessen]** Kollidiert der Webhook-Pfad mit einem aktiven Workflow, kommt beim Publish 409. Quelle: p14.py.
- **F-VAL5 [dokumentiert]** Die Server-Prüfung besteht aus `validateNodeCredentials`, `NodeHelpers.getNodeParametersIssues` und dem Trigger-Check. Deaktivierte Knoten und unverbundene Nicht-Trigger überspringt sie. Quelle: n8n@2.39.9 `dist/workflows/workflow-validation.service.js:32-117`.
- **F-VAL6 [gemessen] nicht mehr tragend** Ein Offline-Nachbau mit `n8n-workflow@2.39.3` trifft alle Server-Ergebnisse, **aber nur, wenn vorher die Defaults gefüllt werden**. Quelle: nt/side/val2.js, dbg.js.
- **F-VAL7 [gemessen] nicht mehr tragend** Gegenprobe mit den Templates 1954, 3050 und 2465:
  - `n8n-workflow` findet 0 Befunde.
  - Das SDK findet 0, 0 und 2 Warnungen.

  Quelle: side/val3.js.
- **F-VAL8 [dokumentiert] nicht mehr tragend** n8n 2.39.9 hängt von `n8n-workflow` 2.39.3 und `@n8n/workflow-sdk` 0.32.3 ab, beide unter der Sustainable Use License. Eine npm-Installation braucht 57 s und 145 MB [gemessen]. Quelle: registry.npmjs.org, LICENSE.md.

## Testlauf über die interne API `/rest` — nicht mehr tragend
Der Testlauf läuft jetzt über `test_workflow` des Instanz-MCP [M-MCP-3]–[M-MCP-6]. Die folgenden Fakten erklären, warum der frühere `/rest`-Weg verworfen ist (design §13).
- **F-RUN1 [gemessen]** `POST /rest/workflows/{id}/run` mit Session-Cookie führt **ausschließlich den gespeicherten** Entwurf aus:
  - Payload-pinData und Payload-Parameter wirken nicht.
  - Gespeicherte pinData wirkt, auch auf Nicht-Trigger-Knoten.

  Quelle: p19.py, p20.py, rv/m1.py (Executions 95–104).
- **F-RUN2 [gemessen]** Knoten, die nur im Payload stehen, ignoriert der Server; es kommt z. B. 500 `Could not find a node named …`. Quelle: p20.py.
- **F-RUN3 [gemessen]** Ohne Trigger und ohne `destinationNode` kommt 400. Quelle: p20.py.
- **F-RUN4 ÜBERHOLT → M5/M7 in design §10.3** Die offenen Punkte zu Schedule-Triggern und AI-Subnodes gelten jetzt für `test_workflow`.
- **F-RUN5 [gemessen, Review]** Ein Webhook-Trigger ohne pinData liefert `{"data":{"waitingForWebhook":true}}`; eine Execution entsteht nicht. Quelle: rv/m3.py.
- **F-RUN6 [gemessen, Review]** Ein PUT, der nur pinData setzt, ändert die `versionId` nicht. Quelle: rv/m2.py.

## Knotenkatalog `/types/*` — nicht mehr tragend
Knotenwissen kommt jetzt aus `search_nodes` und `get_node_types` des Instanz-MCP [M-MCP-25][M-MCP-32]. Die folgenden Fakten gelten weiter.
- **F-NOD1 [gemessen]** `/types/nodes.json` und `/types/credentials.json` liefern 401 ohne Auth **und** mit API-Key. 200 gibt es nur mit dem Session-Cookie. Quelle: nt/p1.py; `dist/server.js:374-385`.
- **F-NOD2 [gemessen, Nachmessung]** `/types/node-versions.json` liefert ebenfalls 401 mit API-Key.
- **F-NOD3 [gemessen]** nodes.json:
  - hat 17.743.676 Bytes, 990 Einträge und 912 Namen,
  - enthält 1.375 `displayOptions`-`@version`-Bedingungen mit `_cnd`-Operatoren.

  Quelle: a1.py, a4.py.
- **F-NOD4 [gemessen; Aussage des Hints durch M-MCP-38 widerlegt]** 161 Knoten haben `builderHint`, 193 Properties haben `propertyHint`. Beim Code-Knoten steht z. B. „LAST RESORT … sandbox has NO network access“. Quelle: a5.py. **Korrektur:** Gemessen stimmt das nur für `fetch` (undefiniert); über `this.helpers.httpRequest` hat Code Netzzugang [M-MCP-38]. Der Hint belegt also nur seinen eigenen Text, nicht die Isolation.
- **F-NOD5 [gemessen]** 4.306 Properties nutzen `loadOptionsMethod`, dazu kommen 1.659 resourceLocator. Quelle: a4.py.
- **F-NOD6 [gemessen]** Ein bedingter GET liefert 304; die ETag hat die Form `W/"size-mtime"`. Quelle: p2.py.
- **F-NOD7 [gemessen]** Ein Suchindex mit einer Zeile pro Knoten kostet ~13,1k Tokens für 512 Knoten und ~26,2k Tokens für alle 912. Quelle: idx.py.
- **F-NOD8 [gemessen]** Kompaktdetails kosten 0,45k–2,4k Tokens. Show und hide dürfen nicht zusammengelegt werden (`sheetName`). Quelle: compact.py.
- **F-NOD9 [gemessen]** Das Trigger-Flag „hat webhooks“ ist falsch; richtig ist `group ∋ trigger`. Quelle: idx.py.
- **F-NOD10 [gemessen]** `executeCommand` und `localFileTrigger` fehlen in nodes.json, weil `excludeNodes` sie ausschließt [F-INST4].
- **F-NOD11 [gemessen, Review]** 54 Einträge sind `hidden: true` und trotzdem im JSON benutzbar. Darunter sind einige mit Datei- oder Code-Wirkung:
  - `n8n-nodes-base.readBinaryFile`, `readBinaryFiles`, `writeBinaryFile`,
  - `function`, `functionItem`,
  - `@n8n/n8n-nodes-langchain.code`, `@n8n/n8n-nodes-langchain.toolHttpRequest`.

  **Weiter tragend** für die Sperrliste (design §8.2). Quelle: work/nodes.json.
- **F-NOD12 [gemessen, Review]** Beim Webhook ist `httpMethod` per Default `GET`; bei `multipleMethods=true` ist der Default `["GET","POST"]`. **Weiter tragend** für `n8n_trigger_workflow`. Quelle: work/nodes.json.
- **F-NOD13 [gemessen, Review]** Diese Knoten haben Außenwirkung **ohne** Credential:
  - `n8n-nodes-base.executeWorkflow` und `@n8n/n8n-nodes-langchain.toolWorkflow` (starten andere Workflows),
  - `rssFeedRead`,
  - `toolHttpRequest`,
  - `git` (Credential optional),
  - `graphql` (Credential je nach `authentication`).

  **Weiter tragend** für die Pinning-Regel (design §3.3). Quelle: work/nodes.json.

## Credentials
- **F-CRED1 [gemessen]** Secrets sind über keine API lesbar. Quelle: nt/p4.py.
- **F-CRED2 [gemessen]** `GET /credentials/schema/{type}` liefert ein JSON-Schema. Quelle: p3.py, p4.py.
- **F-CRED3 [gemessen]** Im Workflow steht eine Credential als `node.credentials[<credType>] = {id, name}`. Beim Publish prüft der Server nur, ob Schlüssel und `id` vorhanden sind, nicht, ob die ID existiert. Quelle: openapi.yml:12049; nt/p6. Dasselbe gilt für den Instanz-MCP [M-MCP-10][M-MCP-11].
- **F-CRED4 [angenommen]** OAuth2 (74 Typen) braucht einen Browser für den Consent.

## Executions
- **F-EXE1 [gemessen]** `GET /executions/{id}?includeData=true` enthält `data.resultData{runData, lastNodeExecuted, error}`. Quelle: p7.py.
- **F-EXE2 [dokumentiert/gemessen]** Die Status-Werte sind canceled, crashed, error, new, running, success, **unknown** und waiting. Quelle: openapi.yml; p12.py. Dieselbe Enum steht im outputSchema von `test_workflow` [M-MCP-H6].
- **F-EXE3 [gemessen]** Retry:
  - Retry einer erfolgreichen Execution ergibt 409.
  - Mit `loadWorkflow:true` kommt nach einem Fix 500.
  - Ein fehlschlagender Webhook-Workflow antwortet mit 500.

  Quelle: p10.py, p11.py.
- **F-EXE4 [gemessen]** Pruning: maxAge 336 h, maxCount 10.000. Quelle: `/rest/settings`. Ein 404 auf eine Execution kann auch von einer Speichereinstellung des Workflows kommen [M-MCP-41].

## Lebenszyklus
- **F-LIFE1 [gemessen]** `DELETE` auf einen aktiven Workflow liefert 200. **Alle Executions werden mitgelöscht.** Quelle: p17.py.
- **F-LIFE2 [gemessen]** Archivieren:
  - `archive` nimmt die Veröffentlichung zurück.
  - PUT und publish auf einen archivierten Workflow ergeben 400.
  - `unarchive` liefert 200.

  Quelle: p18.py.
- **F-LIFE3 [gemessen]** `unpublish`/`deactivate` sind idempotent. Quelle: p18.py.

## Tags
- **F-TAG1 [gemessen]** Tags über die Public API:
  - `POST /tags` liefert 201.
  - `PUT /workflows/{id}/tags` erwartet `[{"id":…}]`.
  - Der Namensfilter ist ein Teiltreffer.

  Quelle: p12.py; bestätigt durch [M-MCP-16].

## Fehlerformen (Public API)
- **F-ERR1 [gemessen]** 400 hat die Form `{"message":"request/<ort>/<feld> <grund>"}`. Quelle: p5.py.
- **F-ERR2 [gemessen]** Ein unbekannter Pfad ergibt 404, ein falscher Content-Type 415. Quelle: p3.py.
- **F-ERR3 [gemessen]** Kaputtes JSON im Request ergibt **500 mit HTML-Body**.

## Lizenz
- **F-LIC1 [gemessen]** Per Lizenz gesperrt (403) sind variables, projects, folders, sourceControl, test-runs und logStreaming. Quelle: p3.py, p12.py, p17.py.
- **F-LIC2 [gemessen, Review]** Aus `/rest/settings`:
  - `advancedPermissions: false`, `customRoles: false`, `sharing: false`, `projects.team.limit: 0`. Es gibt also nur einen Owner und Member, aber keine Admin-Rolle.
  - `communityNodesEnabled: true`, `unverifiedCommunityNodesEnabled: true`.
  - `mfa.enabled: true`, `enforced: false`.

  Quelle: rv_lic.py.

## Brücke
- **F-BR1 [gemessen]** n8n erreicht per HTTP Request andere Hosts in seinem Netz. Mitgesendet werden `User-Agent: n8n` und eigene Header. Quelle: connect, Execution 75.
- **F-BR2 [gemessen]** In Ausdrücken stehen `$execution.id`, `$workflow.id` und `$execution.resumeUrl` zur Verfügung. Quelle: connect.
- **F-BR3 [gemessen]** URL-Formen:
  - Webhook: `<base>/webhook/<path>`,
  - Test: `/webhook-test/<path>`,
  - Wait: `/webhook-waiting/<execId>`.

  Quelle: `/rest/settings`; p6.py.
- **F-BR4 [gemessen]** Die Antwort des Produktions-Webhooks enthält **keine** Execution-ID. Ist der Workflow nicht veröffentlicht, kommt 404 „not registered“. Quelle: p6.py.
- **F-BR5 [gemessen]** Webhook plus Respond to Webhook liefert eigenes JSON zurück, auch `executionId`. Quelle: connect.
- **F-BR6 [gemessen]** `settings.errorWorkflow` feuert **nur, wenn der Error-Workflow veröffentlicht ist**. Die Payload enthält `execution{id,url,error,lastNodeExecuted,mode}` und `workflow{id,name}`. Quelle: connect, Executions 77–80. Der Instanz-MCP erzwingt das beim Setzen [M-MCP-15].
- **F-BR7 [gemessen]** Die Wait-`resumeUrl` ist HMAC-signiert: ohne `signature` 401, mit Signatur 200. Solange die Execution wartet, steht `status: waiting`. Quelle: connect, Execution 76.
- **F-BR8 [gemessen]** Der HTTP-Request-Knoten ohne `timeout`-Option bricht nach 300 s ab. Quelle: connect, Executions 90/91.
- **F-BR9 [gemessen]** `lmChatOpenAi` v1.3 hat per Default `timeout` 60.000 ms und `maxRetries` 2. Quelle: connect `cx_llm.py`.

## MCP-Server-Trigger (Workflow als MCP-Server)
- **F-MCP1 [gemessen]** Unser `mcp_client` (`transport: streaming`) spricht mit einem n8n-MCP-Server-Trigger v2.x unter `<base>/mcp/<path>`:
  - Verbinden, Listen und Aufrufen funktionieren.
  - Der Toolname ist der Knotenname.
  - Jeder Aufruf erzeugt eine Execution.
  - Bearer-Auth: Ein falscher Token ergibt 403.

  Quelle: probe_mcp_trigger.py.
- **F-MCP2 [gemessen]** `transport: sse` liefert 404. Quelle: probe_mcp_trigger.py.
- **F-MCP3 [dokumentiert]** Remote-Server deklariert man in `config/mcp_servers.yaml` unter `external_servers.remote_servers`. Quelle: `mcp_servers.yaml:26-31`; `config/models.py:959-1000`.
- **F-MCP4 ÜBERHOLT → M-MCP-H1…, M-MCP-1…** Früher hieß es: „Instanz-MCP aus, Tools ungemessen.“ Jetzt ist er eingeschaltet und gemessen. Gültig bleibt: Ist der MCP aus, antwortet der Endpunkt 404 „MCP access is disabled“.
- **F-MCP5 ÜBERHOLT → M-MCP-H2** Der Probe-Key von damals ist rotiert. Den Endpunkt dafür kennen wir jetzt.

## Instanz-MCP `/mcp-server/http` — Hauptsession
- **M-MCP-H1 [gemessen, Hauptsession]** Einschalten per `PATCH /rest/mcp/settings {"mcpAccessEnabled": true}` mit Owner-Session liefert 200 `{"mcpAccessEnabled":true,"autoExposeNewWorkflows":false}`.
  - Der Zustand steht in `GET /rest/module-settings` → `mcp`.
  - `GET /rest/mcp/settings` ergibt 404.
  - Auf der Test-Instanz ist der MCP eingeschaltet.

  Den Weg über Env-Variablen beschreibt [M-MCP-22]; das Design nutzt ihn statt des PATCH.
- **M-MCP-H2 [gemessen, Hauptsession]** Der MCP-Key:
  - `GET /rest/mcp/api-key` liefert ihn **nur maskiert** (10 Zeichen), sobald er existiert.
  - `POST /rest/mcp/api-key/rotate` liefert 200 mit einem neuen **Roh-Key** (272 Zeichen); der alte Key ist danach ungültig [angenommen: Rotation heißt Ersetzen].
  - Das Key-Objekt hat die Felder apiKey, audience, createdAt, id, label, lastUsedAt, scopes, updatedAt und userId.
- **M-MCP-H3 [gemessen, Hauptsession]** Der Endpunkt `<base>/mcp-server/http`:
  - spricht streamable HTTP mit JSON-RPC,
  - verlangt `Authorization: Bearer <MCP-Key>`,
  - meldet sich als `{"name":"n8n MCP Server","version":"1.1.0"}`,
  - nutzt Protokoll 2025-06-18.

  Antworten kommen als JSON oder SSE. **Korrigiert → M-MCP-54:** Eine Session-ID im Header `mcp-session-id` sendet 2.39.9 nicht; der Server ist zustandslos.
- **M-MCP-H4 [gemessen, Hauptsession]** Der Server hat 35 Tools:
  - search_workflows, execute_workflow, get_workflow_execution, search_workflow_executions, get_workflow_details,
  - get_workflow_history, get_workflow_version, get_workflow_versions_diff,
  - publish_workflow, unpublish_workflow,
  - prepare_workflow_pin_data, test_workflow,
  - list_credentials, list_n8n_gateway_services, list_workflow_tags,
  - search_data_tables, create_data_table, rename_data_table, add_data_table_column, delete_data_table_column, rename_data_table_column, add_data_table_rows, get_data_table_rows,
  - search_nodes, get_node_types, get_workflow_best_practices, explore_node_resources,
  - validate_workflow, validate_node_config, create_workflow_from_code,
  - search_projects, archive_workflow, update_workflow, restore_workflow_version, get_workflow_sdk_reference.

  Ein Delete-Tool gibt es nicht.
- **M-MCP-H5 [gemessen, Hauptsession]** Gebaut wird in SDK-Code, nicht in JSON:
  - `import { workflow, node, trigger } from '@n8n/workflow-sdk'`,
  - `create_workflow_from_code(code, skillsUsed, name, description, versionName, versionDescription, projectId, folderId)`,
  - `update_workflow(workflowId, skillsUsed, operations, versionName, versionDescription)` mit atomaren Operationen.
- **M-MCP-H6 [gemessen, Hauptsession; teilweise ÜBERHOLT durch M-MCP-30]** Signatur: `test_workflow(workflowId, pinData PFLICHT, triggerNodeName?, timeout ≤3600, Default 300)`.
  - Items müssen in `{"json":…}` stehen.
  - Die Tool-Beschreibung behauptet, Trigger, Credential- und HTTP-Request-Knoten würden gepinnt; Set, If, Code und credential-freie I/O-Knoten liefen normal.
  - Laut Beschreibung unterbricht `timeout` die Test-Execution [dokumentiert, Tool-Beschreibung].
  - Die outputSchema-Status-Enum ist dieselbe wie in F-EXE2 [gemessen, t8.py].
- **M-MCP-H7 [gemessen, Hauptsession; Größe korrigiert durch M-MCP-26]** `search_nodes` für 2 Suchbegriffe: 12.155 Zeichen. Die SDK-Referenz nennt die Hauptsession mit 101.547 Zeichen; das ist ein JSON-Dump [M-MCP-26].
- **M-MCP-H8 [gemessen, Hauptsession]** `validate_node_config` („Schema-level only“):
  - **Erkannt:** Webhook `httpMethod "FROB"`, httpRequest `method "FOO"`, Slack message/post ohne `text`.
  - **Übersehen (valid):** Webhook typeVersion 99, Set mit unbekanntem Parameter, leerer Webhook-Pfad, unbekannter Knotentyp.
- **M-MCP-H9 [gemessen, Hauptsession]** `validate_workflow(code)`:
  - **Erkannt:** unbekannter Knotentyp („Unrecognized node type“).
  - **Übersehen (valid):** Webhook-Version 99, leerer Webhook-Pfad.
  - Kontrollfall: `{"valid":true,"nodeCount":2}`.

## Instanz-MCP — Messung vom 21.09. (work2/t1–t7)
- **M-MCP-1 [gemessen]** `create_workflow_from_code` liefert `{workflowId, name, nodeCount, url, autoAssignedCredentials[], targetProject{id,name,type}}`.
  - Optional kommen targetFolder, note, skippedGroups, hint und warnings dazu.
  - Der Inhalt steht doppelt, in `content[0].text` und in `structuredContent`.
  - `url` hat die Form `<base>/workflow/<id>`.
  - `targetProject.type` ist `personal`, das persönliche Projekt des Key-Besitzers.

  Quelle: t1.py; outputSchema aus tools/list.
- **M-MCP-2 [gemessen]** `prepare_workflow_pin_data` liefert `{nodeSchemasToGenerate, nodesWithoutSchema, nodesSkipped, coverage}`: nur Schemas, nie pinData selbst.
  - Frischer Workflow manual→Set→Code→NoOp: `nodesWithoutSchema=['Start']`, `nodesSkipped=['SetVals','Marker','End']`.
  - Webhook+Slack: `nodesWithoutSchema=['Hook','Slack']`.
  - Ob nach früheren Executions Schemas kommen, ist ungemessen (Messpunkt M13).

  Quelle: t1b.py, t2b.py.
- **M-MCP-3 [gemessen]** pinData des Aufrufers auf Set- und Code-Knoten wird befolgt.
  - Mit Pins auf Set und Code verschwindet der Live-Marker `CODE_LIVE_MARKER`; ausgegeben wird der gepinnte Wert `CODE_PINNED`.
  - Folgeknoten bekommen die gepinnten Daten.
  - HTTP Request: siehe [M-MCP-39]. Andere Typen sind ungemessen.

  Quelle: Execution 105 (Pins wie vorbereitet: Marker live) gegen 106 (Pins auf SetVals und Marker).
- **M-MCP-4 [gemessen]** `test_workflow` liefert nur `{executionId, status[, error]}` (157 Zeichen inklusive Hülle), keine Knotenergebnisse.
  - Die Execution wird im Modus `manual` gespeichert (sofern die Speichereinstellungen es zulassen [M-MCP-41]).
  - Lesbar ist sie per Public API `GET /api/v1/executions/{id}?includeData=true` (200, ~3,4 KB mit runData je Knoten).
  - Ebenso per MCP `get_workflow_execution`: 242 Zeichen Metadaten, mit `includeData=true` 2.095 Zeichen, filterbar über `nodeNames`/`truncateData`.

  Quelle: t1b.py, t1c.py.
- **M-MCP-5 [gemessen]** `test_workflow` führt den **unveröffentlichten Entwurf** aus.
  - Das klappt auf einem nie veröffentlichten Workflow (`activeVersionId` null).
  - Auf einem veröffentlichten Workflow mit später geändertem Entwurf lief im Test der Entwurf (`V2_DRAFT`), der Produktions-Webhook dagegen die veröffentlichte Version (`V1`).

  Quelle: t1c.py, Executions 108/109.
- **M-MCP-6 [gemessen]** Fehlerformen:
  - Ein fehlschlagender Knoten ergibt `{executionId:'107', status:'error', error:'PROBE_BOOM [line 1]'}`: nur die Meldung, ohne Knotenname und **ohne** MCP-`isError`. Die `executionId` ist gesetzt; das unterscheidet ein fehlgeschlagenes Testergebnis von einem Tool-Fehler.
  - `test_workflow`, `execute_workflow` und `publish_workflow` auf einem nicht freigegebenen Workflow liefern ein normales Ergebnis mit `status`/`success=false` plus `error`, ohne `isError`.
  - `get_workflow_details`, `update_workflow`, `archive_workflow`, `prepare_workflow_pin_data` und `get_workflow_history` setzen `isError=true`.

  Quelle: t1c.py, t4.py.
- **M-MCP-7 [gemessen]** Rückgaben von Veröffentlichen und Zurücknehmen:
  - `publish_workflow` liefert `{success:true, workflowId, activeVersionId}`, `unpublish_workflow` liefert `{success:true, workflowId}`.
  - Erfolgreich veröffentlicht wurde auch ein Webhook-Workflow mit `responseMode responseNode` ohne Respond-Knoten, mit Geister-Credential und mit unbalanciertem Ausdruck.

  Quelle: t1c.py, t2b.py.
- **M-MCP-8 [gemessen]** `validate_workflow` meldet `valid:true`, auch wenn seine Warnungen echte Fehler beschreiben. Befunde stehen nur in `warnings[] {code,message,nodeName}`:
  - AI Agent ohne Model-Subnode: `valid:true` + `MISSING_REQUIRED_INPUT` (+ `AGENT_STATIC_PROMPT`, `AGENT_NO_SYSTEM_MESSAGE`).
  - Set-Assignments als String: `valid:true` + `SET_INVALID_ASSIGNMENT` + `INVALID_PARAMETER`.
  - `includeOtherFields:'yes'`: `valid:true` + `INVALID_PARAMETER`.

  Quelle: t2.py.
- **M-MCP-9 [gemessen]** `validate_node_config`:
  - **Erkannt (valid:false):** AI Agent ohne Subnodes (Pfad `subnodes`), Set-Assignments mit falschem Typ, Set `includeOtherFields` mit falschem Typ.
  - **Übersehen (valid:true):** Credential mit nicht existierender ID (das Tool hat gar kein Credential-Feld), unbalancierter Ausdruck `={{ $json.a`, number-Assignment mit Wert `'abc'`, Webhook `responseMode responseNode`.

  Quelle: t2.py.
- **M-MCP-10 [gemessen]** `validate_workflow` hat folgende Fälle **übersehen**; alle ergaben `valid:true` ohne Warnung:
  - Credential-Referenz mit nicht existierender ID,
  - falscher Credential-Typ-Schlüssel (`githubApi` auf Slack),
  - Webhook `responseNode` ohne Respond-Knoten,
  - unbalancierter Ausdruck `={{ $json.a`,
  - Set-number-Assignment mit Text.

  Quelle: t2.py.
- **M-MCP-11 [gemessen]** Folgen zur Laufzeit:
  - `create_workflow_from_code` speichert eine nicht existierende Credential-ID unverändert (`{slackApi:{id:'doesNotExist999',name:'Ghost'}}`), ohne Warnung.
  - Der unbalancierte Ausdruck `={{ $json.a` ergab zur Laufzeit still `null` (Set-Ausgabe `{x:null}`, Status success).

  Quelle: t2b.py; Execution 110.
- **M-MCP-12 [gemessen]** So angelegte Workflows sind unveröffentlichte Entwürfe (`active:false`, `activeVersionId:null`). Sie haben `settings {executionOrder:'v1', availableInMCP:true}` und liegen ohne `projectId` im persönlichen Projekt des Key-Besitzers. Quelle: t1b.py, Public GET.
- **M-MCP-13 [gemessen]** `update_workflow` kennt diese Operationstypen:
  - updateNodeParameters, setNodeParameter (JSON-Pointer), addNode, removeNode, renameNode,
  - addConnection, removeConnection,
  - setNodeCredential, setNodePosition, setNodeDisabled, setNodeSettings,
  - setWorkflowMetadata, setWorkflowSettings,
  - addTags, removeTags, setNodeGroups.

  Weitere Eigenschaften:
  - Höchstens 100 Operationen pro Aufruf, atomar.
  - `setWorkflowSettings` umfasst errorWorkflow, timezone, executionOrder, save*-Flags (saveManualExecutions, saveData*Execution, saveExecutionProgress), executionTimeout, timeSavedPerExecution, callerPolicy und callerIds, aber nicht `availableInMCP`.
  - Das Ergebnis enthält `validationWarnings` (mit `preExisting`).

  Quelle: inputSchema/outputSchema aus tools/list (schemas.py, r1_tools.json).
- **M-MCP-14 [gemessen]** Tags gehen über MCP: `update_workflow addTags` mit einem unbekannten Namen legt den Tag an und hängt ihn an. Sichtbar ist er per Public `GET /workflows/{id}/tags`, `list_workflow_tags` und im Tag-Filter von `search_workflows`. `create_workflow_from_code` hat kein Tag-Feld. Quelle: t3.py; zum Feldnamen siehe [M-MCP-31].
- **M-MCP-15 [gemessen]** `setWorkflowSettings errorWorkflow` wird auf dem Server geprüft.
  - **Abgelehnt:** eine unbekannte ID, ein unveröffentlichter Workflow („has no published version“) und ein veröffentlichter Workflow ohne Error Trigger („has no active Error Trigger node“).
  - **Akzeptiert:** ein veröffentlichter Workflow mit `n8n-nodes-base.errorTrigger`.

  Quelle: t3.py, t3b.py, t3c.py.
- **M-MCP-16 [gemessen]** Public API:
  - `PUT /workflows/{id}/tags` braucht Tag-IDs; der Body `[{name}]` ergibt 400.
  - `PUT /workflows/{id}` nimmt `settings.errorWorkflow='notAWorkflowId'` ohne Prüfung an (200) und behält `availableInMCP`.

  Quelle: t3.py, t3b.py.
- **M-MCP-17 [gemessen]** Der MCP-Key hat:
  - `scopes = []`,
  - audience `mcp-server-api`,
  - label `MCP Server API Key`,
  - als `userId` den Owner (Rolle global:owner).

  Er handelt als dieser Nutzer. Quelle: t4.py.
- **M-MCP-18 [gemessen]** `GET /rest/module-settings` liefert für `mcp` den Wert `{mcpAccessEnabled:true, mcpManagedByEnv:false, serverUrl:'<base>/mcp-server/http', autoExposeNewWorkflows:false}`. Quelle: t4.py.
- **M-MCP-19 [gemessen]** `search_workflows` listet **alle** Workflows, die der Nutzer des Keys sieht, auch nicht freigegebene. Jede Vorschau trägt `availableInMCP`, `tags`, `triggerCount` und `parentFolderId`. Quelle: t4.py.
- **M-MCP-20 [gemessen]** Es gibt ein Freigabe-Tor pro Workflow.
  - Ein per Public API angelegter Workflow hat kein `availableInMCP`.
  - Darauf schlagen `get_workflow_details`, `prepare_workflow_pin_data`, `test_workflow`, `execute_workflow`, `get_workflow_history`, `update_workflow`, `publish_workflow` und `archive_workflow` alle fehl: „Workflow is not available in MCP. …“

  Quelle: t4.py.
- **M-MCP-21 [gemessen]** Die Freigabe geht auch ohne UI: Public `PUT /api/v1/workflows/{id}` mit `settings {executionOrder:'v1', availableInMCP:true}` liefert 200. Danach funktionieren `get_workflow_details` und `test_workflow` (Execution 111). Quelle: t4b.py.
- **M-MCP-22 [gemessen, Hauptsession]** Env-Variablen zum Einschalten beim Start sind `N8N_MCP_MANAGED_BY_ENV=true` **und** `N8N_MCP_ACCESS_ENABLED=true`; beide stehen per Default auf false und existieren ab 2.20.0.
  - Ohne `MANAGED_BY_ENV` wird `ACCESS_ENABLED` ignoriert.
  - Mit `MANAGED_BY_ENV` wird der Wert bei jedem Start neu gesetzt, und der UI-Schalter ist read-only.

  Quelle: docs.n8n.io (manage-settings-using-environment-variables); Quelltext n8n@2.39.9: `packages/@n8n/config/src/configs/instance-settings-loader.config.ts` und `packages/cli/src/instance-settings-loader/loaders/mcp-settings.loader.ts`. Gemessen: Nach dem Neuanlegen mit beiden Variablen zeigt `/rest/module-settings` `mcpAccessEnabled: true, mcpManagedByEnv: true`.
- **M-MCP-23 [dokumentiert]** Für den MCP-Key und für `autoExposeNewWorkflows` gibt es in 2.39.9 keine Env-Variable; letzteres ist eine DB-Einstellung.
  - Weitere Variablen: `N8N_MCP_SERVER_RATE_LIMIT` (Default 100 pro IP und 5 min, 0 schaltet ab) und `N8N_MCP_BASE_URL`.
  - `N8N_MCP_SERVER_SESSION_IDLE_TTL_MS` gehört zum MCP-Server-Trigger, nicht zum Instanz-MCP.
  - Für das Bootstrapping gibt es `N8N_INSTANCE_OWNER_MANAGED_BY_ENV/EMAIL/PASSWORD_HASH`.

  Quelle: n8n@2.39.9, `packages/cli/src/modules/mcp/mcp.config.ts`, `mcp.settings.service.ts`, `packages/@n8n/config/src/configs/mcp-server.config.ts`.
- **M-MCP-24 [gemessen]** Rate-Limit: 100 HTTP-Requests pro IP in 5 Minuten auf `/mcp-server/http`.
  - Danach kommt 429 `{message:'Too many requests'}` mit `x-ratelimit-limit 100`, `x-ratelimit-remaining 0` und `retry-after` ~102 s.
  - Jeder POST zählt: initialize, notifications/initialized und tools/call. Eine frische Session pro Aufruf kostet also 3 Requests.

  Quelle: t7.py.
- **M-MCP-25 [gemessen]** `get_node_types` braucht Objekte `{nodeId, version?, resource?, operation?, mode?}`; ein reiner String ergibt `isError` (Eingabevalidierung). Textgrößen:
  - Slack ohne Diskriminatoren: 644 Zeichen, eine Fehlermeldung mit der Liste der Ressourcen und Operationen,
  - Slack message/post: 9.379,
  - Webhook: 6.833,
  - httpRequest: 17.262,
  - alle drei zusammen: 24.715.

  Quelle: t6.py, t6b.py, t7.py.
- **M-MCP-26 [gemessen]** Textgrößen von `get_workflow_sdk_reference` je Abschnitt:

  | Abschnitt | Zeichen |
  |---|---|
  | patterns | 15.699 |
  | patterns_detailed | 11.905 |
  | expressions | 3.778 |
  | functions | 2.012 |
  | rules | 8.169 |
  | import | 337 |
  | guidelines | 1.678 |
  | design | 1.628 |
  | alles | 49.400 (~12k Tokens) |

  Die 101.547 Zeichen aus [M-MCP-H7] sind der JSON-Dump mit dem doppelten `structuredContent`. Quelle: t6b.py, t7.py.
- **M-MCP-27 [gemessen]** `get_workflow_best_practices`:
  - Textgrößen: list 1.835, notification 5.273, chatbot 5.983 Zeichen.
  - Enum von `technique` laut inputSchema: **list** (Übersicht), scheduling, chatbot, form_input, scraping_and_research, monitoring, enrichment, triage, content_generation, document_processing, data_extraction, data_analysis, data_transformation, data_persistence, notification, knowledge_base, human_in_the_loop, web_app.

  Quelle: t6b.py; inputSchema (r1_tools.json, Review 2: `list` steht an erster Stelle).
- **M-MCP-28 [gemessen]** Aufräumen nach t1–t7: 5 `zz-probe`-Workflows und 1 `zz-probe`-Tag per Public API gelöscht. Danach gab es 0 Workflows und 0 Executions; die MCP-Einstellungen sind unverändert.
- **M-MCP-29 [angenommen]** Wie sich der MCP-Key eines Nicht-Owners verhält (Projekt-Sichtbarkeit), ist **nicht gemessen**. Dafür bräuchte es einen zweiten Nutzer, also eine Instanz-Änderung. Angenommen wird, dass er mit den Projektrechten dieses Nutzers handelt.

## Instanz-MCP — Nachmessung für den Designumbau (work2/t8–t9b, 21.09.)
- **M-MCP-30 [gemessen]** Ein **ungepinnter HTTP-Request-Knoten läuft in `test_workflow` live**.
  - Aufbau: manual→httpRequest `GET http://localhost:5678/healthz`, pinData nur auf dem Trigger.
  - Ergebnis: `Health` lieferte `{status:'ok'}` mit executionStatus success (Execution 112).
  - Die Behauptung der Tool-Beschreibung, HTTP-Knoten würden gepinnt [M-MCP-H6], erzwingt der Server also nicht. Gepinnt wird, was der Aufrufer pinnt.

  Quelle: t8.py.
- **M-MCP-31 [gemessen]** Tags über MCP im Detail:
  - Die Operation heißt `{"type":"addTags","names":[…]}`. Mit falschem Feldnamen kommt das Ergebnis `{"error":"Invalid operations: operation 0.names: Required"}`, ohne `isError`.
  - Das `update_workflow`-Ergebnis hat die Schlüssel appliedOperations, autoAssignedCredentials, name, nodeCount, url, validationWarnings und workflowId.
  - `get_workflow_details(detailLevel:'execution')` enthält `workflow.tags [{id,name}]` und ist 893 Zeichen groß.
  - Die Schlüssel von `workflow` sind active, activeVersion, activeVersionId, canExecute, connections, createdAt, id, isArchived, meta, name, nodeCount, nodeGroups, nodes, parentFolderId, scopes, settings, tags, triggerCount, updatedAt und versionId.

  Quelle: t8.py, t8b.py.
- **M-MCP-32 [gemessen]** `get_node_types` verlangt `version` als **String**; die Zahl `2.1` ergibt isError (Eingabevalidierung).
  - Eine existierende Version (`"2.1"`) liefert 7.081 Zeichen TypeScript-Definition.
  - Eine nicht existierende Version (`"99"`, `"9.9"`) liefert **ohne isError** den Text `# Errors … Version '99' not found for node 'n8n-nodes-base.webhook'` (102 Zeichen).

  Quelle: t9.py, t9b.py.
- **M-MCP-33 [gemessen]** Ein Set-number-Assignment mit Wert `'abc'` scheitert **im Testlauf**: `test_workflow` meldet `status:error` mit `"'n' expects a number but we got 'abc' [item 0]"` (Execution 113). Beide Validatoren übersehen den Fall [M-MCP-9][M-MCP-10], der Test fängt ihn. Quelle: t9.py.
- **M-MCP-34 [gemessen]** Ein Webhook-Trigger mit `responseMode: responseNode` ohne Respond-Knoten läuft **im Testlauf grün**: gepinnter Trigger, `status: success` (Execution 114). Erst der Produktivaufruf scheitert, mit einem Laufzeit-500 [F-VAL3]. Validatoren und Test übersehen den Fall also beide. Außerdem belegt der Lauf, dass ein Webhook-Trigger per pinData testbar ist. Quelle: t9.py.
- **M-MCP-35 [dokumentiert]** Die Spec der Public API widerspricht der Messung bei `availableInMCP`. Laut Spec muss der Workflow dafür „active“ sein und einen aktiven Webhook-Knoten haben. Gemessen ist das Flag aber auch auf unveröffentlichten Workflows mit manuellem Trigger gesetzt und wirksam [M-MCP-12][M-MCP-21]. Die Messung gilt. Quelle: openapi.yml (`settings.availableInMCP`).
- **M-MCP-36 [gemessen]** Die Aufrufe von t8 bis t9b blieben ohne 429. Eine Session pro Skript kostete 2 Requests plus 1 Request je Tool-Aufruf. Quelle: t8.py–t9b.py.
- **M-MCP-37 [gemessen]** Aufräumen nach t8–t9: 4 `zz-probe`-Workflows (live, tag, num, resp) und der Tag `zz-probe-managed` sind gelöscht (je 200). Danach gab es 0 `zz-probe`-Workflows und 0 `zz-probe`-Tags. Mit den Workflows sind die Executions 112–114 gelöscht [F-LIFE1]. Die Instanz-Einstellungen sind unverändert.

## Instanz-MCP — Review-Runde 2 (work2/r1–r6, 21.09.)
- **M-MCP-38 [gemessen, Review 2]** Ein **Code-Knoten hat Netzzugang** über `this.helpers.httpRequest`.
  - Aufbau: manual→Code `Net` mit `await this.helpers.httpRequest({url:'http://localhost:5678/healthz', json:true})`, nur der Trigger gepinnt.
  - Ergebnis: `Net` lieferte `{helpers:{status:'ok'}}` (Execution 115). `fetch` ist im Code-Knoten undefiniert.
  - Gemessen mit dem generischen Compose-Aufbau (`N8N_RUNNERS_ENABLED: "true"` [F-DEP1]).
  - Widerlegt die Aussage des builderHints „sandbox has NO network access“ [F-NOD4].

  Quelle: r2.py.
- **M-MCP-39 [gemessen, Review 2]** pinData auf einem **HTTP-Request-Knoten** wird befolgt: `PinnedHttp` mit URL `http://localhost:1/never` lieferte den gepinnten Wert `{pinned:'HTTP_PINNED'}`, Status success; ein Live-Aufruf auf Port 1 wäre gescheitert (Execution 115). Quelle: r2.py.
- **M-MCP-40 [gemessen, Review 2]** `n8n-nodes-base.executeWorkflow` v1.2 mit `source: 'parameter'` führt ein **Inline-Workflow-JSON** aus: Knoten `Inline` mit `workflowJson` = executeWorkflowTrigger→Set lieferte `{inline:'INLINE_RAN'}` (Execution 115). Laut `get_node_types(executeWorkflow)` kennt `source` auch `localFile` (Text enthält `workflowJson`, `localFile`). Die Knoten im Inline-JSON stehen nicht in `nodes[]` des äußeren Workflows. Quelle: r2.py.
- **M-MCP-41 [gemessen, Review 2; eingegrenzt → M-MCP-55]** `update_workflow setWorkflowSettings {saveManualExecutions:false, saveDataSuccessExecution:'none'}` wird angenommen. Danach liefert `test_workflow` `{executionId:'117', status:'success'}`, aber Public `GET /executions/117?includeData=true` ergibt **404**: Die Execution ist nicht gespeichert. Quelle: r2.py; die Schlüssel stehen im Schema von `update_workflow` (r1_tools.json). Welche der beiden Einstellungen es war, trennt erst M-MCP-55.
- **M-MCP-42 [gemessen, Review 2]** Agent-Tool-Varianten: `search_nodes(usage:'agentTool')` liefert u. a. `n8n-nodes-base.httpRequestTool`, `graphqlTool`, `gitTool`, `rssFeedReadTool`, `s3Tool`, `gmailTool`, `googleSheetsTool`, `githubTool`, `gitlabTool`, `npmTool`, `@n8n/n8n-nodes-langchain.toolCode`, `toolVectorStore`, `toolSerpApi` und `@n8n/n8n-nodes-langchain.mcpRegistryClientTool`. Das Muster ist `<basistyp>Tool` bei `n8n-nodes-base`, Präfix `tool…` bei den langchain-Knoten. Quelle: r3.py, r4.py (am 21.09. erneut ausgeführt, gleiche Namen).
- **M-MCP-43 [gemessen, Review 2]** `get_node_types(sort)` zeigt `type` mit den Werten `simple`, `random` und `code`; `merge` hat den Modus `combineBySql`. Ein Sort-Knoten mit `type:'code'` lief ungepinnt live und veränderte die Items (Execution 119). Im Sort-Code sind `typeof process` und `typeof require` `'undefined'`; Umgebungsvariablen waren nicht sichtbar. Netzzugang aus Sort-Code und Dateifunktionen in `combineBySql` sind **ungemessen**. Quelle: r5.py, r6.py.
- **M-MCP-44 [gemessen, Review 2]** Ein `get_node_types`-Aufruf mit einem gültigen und einem ungültigen Knoten (Version `'99'`) liefert **ohne isError** einen Text, der mit `# TypeScript Type Definitions` beginnt; der Abschnitt `# Errors` mit `Version '99' not found …` steht erst ab Zeichen 6.423. Ein Einzelaufruf mit ungültiger Version beginnt dagegen direkt mit `# Errors` [M-MCP-32]. Quelle: r2.py.
- **M-MCP-45 [gemessen]** Stand nach Review-Runde 2: Public `GET /workflows?limit=250` liefert 0 Workflows, `GET /tags` 0 Tags (21.09., lesend geprüft). Mit den Workflows sind die Executions 115–119 gelöscht [F-LIFE1].

## Instanz-MCP — Bau Phase 1a (Hauptsession, 21.09.)
- **M-MCP-46 [gemessen, Hauptsession]** `newCredential('Slack Bot')` im SDK-Code, ohne dass ein solches Credential existiert: Der gespeicherte Knoten hat **kein** `credentials`-Feld; `autoAssignedCredentials` ist leer. Die Prüfung `CREDENTIAL_UNKNOWN_ID` greift darauf nicht, das fehlende Credential ist eine Aufgabe für den Nutzer. Quelle: newcred.py.
- **M-MCP-47 [gemessen, Hauptsession]** Beantwortet M7, Teil 1: Ein **gepinnter AI-Agent-Knoten ruft seine Subnodes nicht auf**. Manual → Agent mit `lmChatOpenAi`-Subnode ohne Credential; nur Trigger und Agent gepinnt: `success`, der Agent liefert den gepinnten Wert (Execution 121). Ein Aufruf des Modells wäre ohne Credential gescheitert. Quelle: m7_m9.py.
- **M-MCP-48 [gemessen, Hauptsession]** Beantwortet M7, Teil 2: Ein **gepinnter Subnode ersetzt das Modell nicht**. Nur der `Model`-Subnode gepinnt, der Agent nicht: `status: error`, „Error in sub-node Model“ (Execution 122). Gepinnt wird deshalb immer die Wurzel, nie ein einzelner Subnode (design §3.3). Quelle: m7_m9.py.
- **M-MCP-49 [gemessen, Hauptsession]** Beantwortet M9: `test_workflow` mit `timeout: 5` auf einem Wait von 25 s antwortet nach 6,3 s mit `{executionId:'123', status:'error', error:'Workflow execution timed out after 5 seconds'}`. Die Execution steht danach auf `canceled` („The execution was cancelled manually“) und bleibt dort. Quelle: m7_m9.py.
- **M-MCP-50 [gemessen, Hauptsession]** `validate_workflow` auf Code, den n8n nicht parsen kann (unbekannter Knotentyp), antwortet mit `isError: true` **und** dem Urteil `{"valid": false, "errors": ["Failed to parse … Unrecognized node type: …"]}`. Wer nur `isError` liest, meldet einen Befund als kaputtes Tool. Quelle: smoke.py.
- **M-MCP-51 [gemessen, Hauptsession]** Ohne `/rest` verrät n8n seine Version nicht: Weder `/healthz`, `/healthz/readiness` noch `/api/v1/…` senden einen Versions-Header, und `serverInfo` des MCP ist `1.1.0` (die MCP-Server-Version). Eine Versionswarnung zur Laufzeit hat damit keine Quelle; `tested_n8n_version` bleibt als Vermerk für Upgrades. Quelle: curl -D.
- **M-MCP-52 [gemessen, Hauptsession]** Das Plugin gegen die Instanz, einmal durch alle Tools: anlegen, taggen und nachprüfen (0 Befunde), Testlauf Execution 120 `success` mit gepinntem Webhook-Trigger und live gelaufenem Set/Respond („Hello Ada“), `removeTags` des eigenen Tags abgelehnt, ein per `setNodeParameter` geleerter Webhook-Pfad sofort als `WEBHOOK_PATH_EMPTY` gemeldet; jeder Aufruf mit genau einer Status-Zeile; danach 0 Workflows. Quelle: smoke.py.

## Instanz-MCP — großes Review Phase 1a (21.09.)
- **M-MCP-53 [gemessen, Hauptsession + Review 3]** Sub-Workflows im Test. B = executeWorkflowTrigger → httpRequest (`/healthz`), A = manualTrigger → executeWorkflow (`source: database`, B):
  - Nur A's Trigger gepinnt: Execution 135 startet B als eigene Execution 136, und B's HTTP-Knoten läuft **live** (Antwort `{"status":"ok"}`). B's Knoten stehen außerhalb von A's Pin-Plan; keine Prüfung am Aufrufer kann sie begrenzen.
  - Trigger **und** Aufrufknoten gepinnt: Execution 138 `success`, der Aufrufknoten liefert den gepinnten Wert, B hat danach **keine** Execution.

  Quelle: review/subwf_probe.py, fix/subwf_pinned.py.
- **M-MCP-54 [gemessen, Hauptsession + Review 3]** Der Instanz-MCP von 2.39.9 ist **zustandslos**: `initialize` sendet keinen Header `mcp-session-id`, und `tools/call` mit falscher oder fehlender Session-ID wird mit 200 beantwortet. Ein Client, der das Handshake an der Session-ID festmacht, schickt es vor jedem Aufruf neu: 3 statt 1 der 100 Requests je 5 Minuten. Ein 429 trifft dann zuerst das `initialize` (retry-after 26 gemessen). Quelle: fix/sess.py, review/p1.py, p2.py, p9.py.
- **M-MCP-55 [gemessen, Hauptsession + Review 3]** Die Speichereinstellungen einzeln, je ein Testlauf:
  - nur `saveManualExecutions: false` → Execution 140, Public GET **404**,
  - nur `saveDataSuccessExecution: 'none'` → Execution 139, gespeichert (Modus `manual`),
  - nur `saveDataErrorExecution: 'none'`, Lauf mit Fehler → Execution 137, gespeichert.

  Einen Testlauf ungespeichert lässt also nur `saveManualExecutions: false`. Quelle: fix/save_manual.py, review/save_probe.py, save_probe_err.py.
- **M-MCP-56 [gemessen, Review 3]** Eine `ai_tool`-Kante von einem **deaktivierten** `toolCalculator` in einen HTTP-Request-Knoten nimmt `update_workflow` an (3 Operationen angewendet). Eine `ai_tool`-Kante von einem NoOp lehnt n8n ab („does not produce an ai_tool output“). Der alte Pin-Plan hielt den HTTP-Knoten deshalb für eine Wurzel mit lauter erlaubten Subnodes und ließ ihn live laufen (Execution 131). Quelle: review/p8.py, p8b.py.
- **M-MCP-57 [gemessen, Review 3]** `get_workflow_details` mit `detailLevel: 'execution'` liefert Metadaten, `versionId`, `nodeCount`, `settings`, `tags` und `triggerInfo`, aber **keine** `nodes` und `connections`. Die liefert nur `'full'`, n8ns eigener Vorgabewert. Quelle: Review 3, Probe auf zz-probe-rv-ops.
- **M-MCP-58 [gemessen, Review 3]** Ein IF, dessen Item in den false-Zweig geht, steht in runData als `main: [[], [{json: …}]]`: Ausgang 0 leer, Ausgang 1 mit dem Item (Execution 129). Quelle: review/p6.py.
- **M-MCP-59 [gemessen, Review 3]** `search_nodes(['mcp client'])` liefert `@n8n/n8n-nodes-langchain.mcpClient` (v1.1, „Standalone MCP Client“), `mcpClientTool` (v1.4, „Connect tools from an MCP Server“) und `mcpRegistryClientTool` („(internal)“). Die ersten beiden rufen eine beliebige MCP-Endpunkt-URL auf; M-MCP-42 kannte nur den dritten. Quelle: Review 3.
- **M-MCP-60 [gemessen, Hauptsession]** `mcpTrigger` v2: `authentication?: 'none' | 'n8nOAuth2' | 'bearerAuth' | 'headerAuth'`, Vorgabe `none` mit dem builderHint „Only select an authentication method when the user explicitly asks“; Credentials `httpBearerAuth` oder `httpHeaderAuth`. Ohne Auth ist ein veröffentlichter MCP-Trigger für jeden offen, der n8n erreicht. Quelle: fix/mcptrigger.py.
- **M-MCP-61 [gemessen, Hauptsession]** `get_workflow_execution` auf eine **fehlgeschlagene** Execution (Code wirft, Execution 147 `error`) antwortet ohne `isError` und ohne `error` auf oberster Ebene: `{execution: {…, status: 'error'}}`, mit `includeData` zusätzlich `data.resultData.error`. Das Lesen eines Fehlschlags ist damit kein Tool-Fehler. Quelle: fix/failed_exec.py.
- **M-MCP-62 [gemessen, lokal: Python gegen Node]** `http://evil.test\@allowed.example.org/x`: Pythons `urlparse(...).hostname` ist `allowed.example.org`, Nodes `new URL()` und `url.parse()` lesen `evil.test`. Die httpRequest-Definition hat dazu `options.proxy` und `options.pagination.pagination.nextURL` (v3–v4.5). Dass n8n die URL so an axios weitergibt, ist geschlossen, nicht live gemessen; die Live-Prüfung lehnt solche URLs deshalb ab. Quelle: Fix-Review, fixreview/u.py, u.js, work/nodes.json. Nachgeprüft im zweiten Fix-Review mit einem Fuzz über 306.880 URL-Formen gegen `new URL` und `url.parse`: Mit Backslash und Steuerzeichen überall, Nicht-ASCII, Leerzeichen, `@` und `%` nur im Host abgelehnt, liefert die Prüfung nie einen erlaubten Host, wo Node einen anderen erreicht. Quelle: fixreview2/fuzz2.py, fuzz2.js.
- **M-MCP-63 [aus dem Knoten-Dump]** Unter den 990 Knotentypen trägt nur `n8n-nodes-base.emailReadImap` die Gruppe `trigger`, ohne auf `Trigger` zu enden (neben webhook, cron, interval). Ohne `triggerNodeName` startet n8n den ersten aktivierten Trigger in Knotenreihenfolge (`findEnabledEligibleTrigger`). Quelle: work/nodes.json, n8n-Quelle `mcp.utils.js`, Fix-Review 2.
- **M-MCP-64 [aus dem Knoten-Dump]** `@n8n/n8n-nodes-langchain.lmChatOpenAi` (v1 bis 1.3) hat ab v1.3 den Schalter `responsesApiEnabled` mit Vorgabe `true` (Responses-API) und unter `options` eine eigene `baseURL`. Ein n8n-Workflow kann damit einen Responses-kompatiblen Endpunkt als Chat-Modell ansprechen. Ob n8n damit einen fremden Endpunkt vollständig bedient, ist nicht gemessen. Quelle: work/nodes.json, E8.

## Phase 1b: Veröffentlichen, Auslösen, Beobachten
- **M-MCP-65 [gemessen]** Public `GET /executions/{id}` trägt `workflowVersionId`: die `versionId` des Entwurfs, auf dem die Execution lief (Testlauf 163 = `versionId` des Workflows). In der **Liste** `GET /executions` steht das Feld auf `null`. `addTags` ändert die `versionId` nicht. Damit prüft publish „dieser Stand ist getestet“ genau. Quelle: fix/p1b.py, fix/p1b2.py.
- **M-MCP-66 [gemessen, beantwortet M11]** Antworten des MCP ohne `isError`:
  - `publish_workflow` mit `versionId` → `{success:true, workflowId, activeVersionId}`; danach `versionId` = `activeVersionId`, `active: true`.
  - Ein zweiter Workflow auf demselben Webhook-Pfad: `{success:false, activeVersionId:null, error:'There is a conflict with one of the webhooks.'}`.
  - Auf einen archivierten Workflow: `{success:false, error:"Workflow '<id>' is archived and cannot be accessed."}`.
  - `unpublish_workflow` zweimal: beide `{success:true, workflowId}`.
  - `archive_workflow` → `{archived:true, workflowId, name}`; ein zweites Mal `isError` mit derselben Meldung wie oben. Danach `isArchived: true`, `active: false`.

  Quelle: fix/p1b.py.
- **M-MCP-67 [gemessen]** Produktions-Webhook und Executions:
  - `responseMode` `onReceived` antwortet sofort `{"message":"Workflow was started"}` ohne Execution-ID, `lastNode` erst nach dem Lauf mit den Daten des letzten Knotens (3 s bei einem Wait von 3 s).
  - Ein unbekannter Pfad: 404 mit `{"code":404,"message":"The requested webhook \"POST <path>\" is not registered.", "hint": …}`.
  - `GET /executions?workflowId=` ohne `status` listet nur **fertige** Executions. Eine laufende steht nur unter `status=running` (Modus `webhook`). `startedAfter` filtert ebenfalls nur fertige.
  - Ein Wait-Knoten ohne `unit` wartet Stunden, nicht Sekunden (die erste Probe blieb stehen und verschwand mit dem Löschen des Workflows).

  Quelle: fix/p1b.py, fix/p1b2.py (Executions 163–168).
- **M-MCP-68 [gemessen]** Public `GET /workflows/{id}` eines veröffentlichten Workflows liefert `activeVersionId` und `activeVersion` mit `nodes` (samt `webhookId`), `connections`, `versionId`, `workflowPublishHistory`. Quelle: fix/p1b.py.
- **M-MCP-69 [gemessen, beantwortet M10]** Unser `mcp_client` gegen einen n8n-MCP-Trigger, der nicht da ist: `connect_all` wirft nicht, meldet je Server einen Fehler (geschlossener Port: `ConnectTimeout` nach 5 s; n8n erreichbar, Pfad unbekannt: `McpError: Session terminated`) und startet weiter. Die Tool-Liste ist leer, ein Aufruf ergibt `MCPConnectionError … is not connected`. Automatisch neu verbunden wird nicht. Quelle: fix/m10.py (`ExternalServerPool`, timeout 5 s).
- **M-MCP-70 [gemessen, 22.09.2026]** Public API: Eine fehlende Execution und ein fehlender Workflow antworten `404` mit `application/json` und `{"message":"Not Found"}`. Der Testlauf 189 (MCP `test_workflow`) steht mit `mode: manual`, der Produktionslauf 190 (Webhook) mit `mode: webhook`, beide mit derselben `workflowVersionId`, weil Entwurf und veröffentlichter Stand gleich waren. Listeneinträge tragen `mode` und `startedAt`. `limit=250` wird angenommen. Quelle: Probe gegen die Testinstanz nach dem E2E-Lauf (e2e_get).

## Einbetten, CORS und Beobachtung
- **F-EMB1 [gemessen/dokumentiert]** Einbetten und Transport:
  - Der Editor sendet `X-Frame-Options: SAMEORIGIN`, fest verdrahtet.
  - Das Cookie `n8n-auth` ist `HttpOnly; SameSite=Lax; Max-Age=604800`; auf der Test-Instanz ohne `Secure` [F-DEP1].
  - Die generische Compose-Datei veröffentlicht Port 5678 direkt, ohne TLS [dokumentiert: `docker-compose.yml:8-9`].

  Quelle: curl; `packages/cli/src/server.ts`.
- **F-EMB2 [gemessen]** `/api/v1`, `/rest` und `/mcp-server/http` senden keine CORS-Header. Quelle: curl OPTIONS.
- **F-OBS1 [gemessen]** Log-Streaming ist lizenzgesperrt. Der Editor-Push lieferte in 25 s 0 Events für Produktivläufe. Quelle: connect.
- **F-OBS2 [gemessen/dokumentiert]** Die OTel-Settings sind lesbar und stehen auf aus. Die External Hooks brauchen eine Änderung am Container. Quelle: `/api/v1/settings/otel`; docs.

## Unsere Seite
- **F-OUR1 [gemessen]** In `config/` und `src/agent_system/` gibt es keinen n8n-Bezug. Ein Plugin-Ordner, der nur Doku enthält, wird von der Discovery still übersprungen (DEBUG). Quelle: probe_discovery.py.
- **F-OUR2 [dokumentiert]** Der Handler-Vertrag verlangt `{"status":"success"|"error"}`; `failed` erkennt das Fehlernetz nicht. Status-Zeilen: genau ein `end` oder `error`, höchstens 140 Zeichen. Quelle: `tools/base.py:86-90`; `tests/plugins/test_status_end_lines.py`.
- **F-OUR3 [dokumentiert]** Das Framework validiert keine Argumente und kappt keine Ergebnisse (außer context_engineer, Pre-Layer T). Quelle: `references/tools.md:71-93`.
- **F-OUR4 [dokumentiert]** `${VAR}` greift nur bei Namen aus `[A-Z0-9_]`. Eine nicht gesetzte Variable ergibt `""` plus WARNING. Quelle: `config/settings.py:29-60,122-143,203-205,300-311`.
- **F-OUR5 [dokumentiert]** Agent- und Tool-YAMLs unter `src/plugins*/*/agents/*.yaml` bindet der Glob in `config/config.yaml:18` ein. Dort stehen sie unter `plugins:` → `servers:`. Quelle: `src/plugins/research/agents/*.yaml`.
- **F-OUR6 [dokumentiert]** httpx und aiohttp sind Core-Abhängigkeiten. Quelle: `requirements/core.txt:9,11`.
- **F-OUR7 [dokumentiert]** `enabled` steht per Default auf false. Plugin-Config wird nicht validiert. Quelle: `config/plugins.yaml:16-17`.
- **F-OUR8 [dokumentiert]** Muster „ohne Key keine Tools“. Quelle: `tavily_search/schema.yaml:4`, `server.py:57-60`.
- **F-OUR9 ÜBERHOLT → F-DEP5** Früher: eine `.env` in `docs/deploy/` wäre nicht git-ignored.
- **F-OUR10 [dokumentiert]** Allowlists: `tools.allowed: ["+…"]` ergänzt die Liste, ohne `+` ersetzt sie. Ein Agent mit visibility `tool`/`both` erscheint für Aufrufer mit `<agent>/*` als `<agent>_execute_task`. Quelle: `config/agents/agents.yaml:6-32`; `runtime.py:154-159`; `tool_discovery.py:156-222`.
- **F-OUR11 [gemessen]** Den Root-SAM `sub_agent_manager` erreicht kein aktiver Agent. Quelle: probe_sam.py.
- **F-OUR12 [dokumentiert]** `wake_blocked()` liefert "" oder einen Grund. `wake_session()` startet einen **neuen** Prozess und wirkt nur, solange der Prozess lebt, der die Arbeit hält. Quelle: `core/session_presence/wake.py`; Muster `terminal/server.py:306-340,441-462`.
- **F-OUR13 [dokumentiert]** Der einzige Teardown ist `stop_plugin` auf dem Objekt aus `PLUGIN_FACTORY`. Quelle: `plugins/capabilities.py:182-190`; `test_pluginsystem_teardown_hook.py:59`.
- **F-OUR14 [dokumentiert]** Die API bindet `127.0.0.1:8000`. Quelle: `config/config.yaml:56-57`.
- **F-OUR15 [dokumentiert]** `/run` antwortet immer mit `text/event-stream`. Quelle: `app.py`, Suchbegriff `media_type="text/event-stream"`.
- **F-OUR16 [dokumentiert]** Plugin-Routen akzeptieren nur JWT oder Cookie, kein `X-API-Key`. Quelle: `plugins/web_adapter.py:335,342`.
- **F-OUR17 [dokumentiert]** API-Keys gibt es nur pro User; einen Service-Account gibt es nicht. Quelle: `api/auth_endpoints.py:393-424`.
- **F-OUR18 [gemessen]** `src/agent_system` stellt keinen MCP-Server bereit. Quelle: grep.
- **F-OUR19 [gemessen]** `validate_plugin.py src/plugins/n8n` endet ohne `plugin.toml` mit Exit 1 und gibt keinen Grund aus. Ursache: `validate()` kehrt bei `:94-95` vor `_report_results()` zurück.
- **F-OUR20 [gemessen]** Allowlists mit einzelnen Tools sind im Repo üblich. Quelle: grep in `config/agents`.
- **F-OUR21 [gemessen, Review]** Kein bestehender Agent erreicht einen anderen über `<agent>/*`. Quelle: grep.

## Extern
- **F-EXT1 [dokumentiert]** `czlonkowski/n8n-mcp` (MIT) baut seinen Katalog aus npm-Paketen, nicht aus der Instanz, und hat die Telemetrie standardmäßig an. Quelle: GitHub README, PRIVACY.md.

## Messpunkte

**Erledigt:**
- M1/M2 (`/rest`-pinData): siehe F-RUN1. Für `test_workflow` beantworten [M-MCP-3] und [M-MCP-39] beide Fragen: Die pinData des Aufrufers wirkt auf Set, Code und HTTP Request.
- M3 (Python-Validator gegen den Server): entfällt, weil kein Python-Nachbau mehr existiert.
- M6: entfällt, weil nicht mehr per PUT geschrieben wird.
- „Endpunkt zum Rotieren des MCP-Keys“: [M-MCP-H2].
- „Verhalten der Instanz-MCP-Tools“: [M-MCP-H1]–[M-MCP-44].
- „Ist die Code-Sandbox netzlos?“: nein [M-MCP-38].
- M12 „Sieht der Lese-Key die Tags?“: ja [F-AUTH8].
- M7 „Wirkt pinData auf AI-Subnodes?“: Der gepinnte Wurzelknoten ruft sie nicht auf [M-MCP-47]; ein gepinnter Subnode ersetzt nichts [M-MCP-48].
- M9 „Was liefert `test_workflow` nach dem Timeout?“: `status: error` mit Timeout-Meldung, Execution `canceled` [M-MCP-49].
- M8 „Wann läuft eine MCP-Session ab?“: entfällt, 2.39.9 vergibt keine Session [M-MCP-54].
- M10 „mcp_client mit unerreichbarem Server“: Start läuft weiter, kein automatisches Neuverbinden [M-MCP-69].
- M11 „Ablehnungen von `publish_workflow`“: `success:false` mit Text, ohne `isError` [M-MCP-66].

**Offen:**
- **M4:** Wie verhält sich der MCP-Key eines Member-Users [M-MCP-29]? Das braucht einen zweiten Nutzer auf der Instanz und damit das OK des Betreibers.
- **M5:** Wie verhält sich `test_workflow` mit Schedule- oder Polling-Trigger (gepinnt)?
- **M13:** Liefert `prepare_workflow_pin_data` nach einer ersten Execution Schemas [M-MCP-2]?

**Dazu ungemessen:**
- das Maximum des HTTP-Timeouts,
- ob `limit>250` gekappt wird,
- wie ein Webhook auf eine falsche HTTP-Methode antwortet,
- wie sich `explore_node_resources` ohne Credential verhält,
- ob die Sandbox von `langchain.code` der des Code-Knotens entspricht,
- ob Sort-Code Netz hat und was `combineBySql` an Dateifunktionen erlaubt [M-MCP-43].
