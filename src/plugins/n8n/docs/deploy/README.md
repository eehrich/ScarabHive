# n8n für ScarabHive bereitstellen

Diese Anleitung richtet eine eigene n8n-Instanz in Docker ein und verbindet sie mit dem ScarabHive-Plugin `n8n`. Sie gilt für jede ScarabHive-Installation. Die Beispiele verwenden `http://localhost:5678`; setze überall deine eigene Adresse ein.

Getestet ist der Aufbau mit n8n 2.39.9 (Community, Docker, SQLite). Die Fakten dazu stehen in `../n8n_facts.md`, das Design in `../design.md`.

## Was hier liegt

| Datei | Zweck |
|---|---|
| `docker-compose.yml` | n8n-Container mit festgelegter Image-Version, Volume `n8n_data`, Instanz-MCP per Env eingeschaltet |
| `.env.example` | Vorlage für deine `.env` |
| `setup_owner.sh` | Einmaliges Setup: Owner, Public-API-Key, MCP-Key, alles nach `CREDENTIALS` |
| `.gitignore` | hält `.env` und `CREDENTIALS` aus Git heraus |

## Voraussetzungen

- Docker mit dem Compose-Plugin (`docker compose version` muss antworten).
- Auf dem Rechner, der das Setup ausführt: `sh`, `curl`, `python3` und `openssl`. Unter Windows geht das in WSL oder Git Bash.
- `setup_owner.sh` spricht n8n unter `http://127.0.0.1:<N8N_PORT>` an. Es muss also **auf dem Docker-Host** laufen.
- Eine frische n8n-Instanz. Hat die Instanz schon einen Owner, braucht das Skript dessen Passwort in `CREDENTIALS` (siehe „Bestehende Instanz“).

## 1. Verzeichnis wählen

Du hast zwei Möglichkeiten:
- **Am Ort ausführen:** direkt in `src/plugins/n8n/docs/deploy/`. `.env` und `CREDENTIALS` sind dort git-ignored und landen nicht in einem Commit.
- **Kopieren:** den Ordner `docs/deploy/` in ein Verzeichnis deiner Wahl kopieren, z. B. auf einen Server. Dann liegen die Geheimnisse nicht im Repository-Baum. Die `.gitignore` kopierst du mit, falls das Zielverzeichnis selbst unter Git steht.

Alle folgenden Befehle laufen in diesem Verzeichnis.

## 2. `.env` anlegen

```sh
cp .env.example .env
chmod 600 .env
```

Dann `N8N_ENCRYPTION_KEY` erzeugen und eintragen:

```sh
openssl rand -hex 32
```

Den ausgegebenen Wert hinter `N8N_ENCRYPTION_KEY=` in `.env` setzen. Ohne diesen Wert startet der Container nicht.

**Wichtig:** Mit diesem Schlüssel verschlüsselt n8n alle gespeicherten Credentials. Bewahre ihn sicher auf und ändere ihn nach dem ersten Start nicht mehr; sonst kann n8n die vorhandenen Credentials nicht mehr entschlüsseln.

Die übrigen Werte:

| Variable | Bedeutung | Default |
|---|---|---|
| `N8N_PUBLIC_URL` | Die Adresse, unter der Browser und Webhook-Aufrufer n8n erreichen, mit abschließendem `/`. n8n baut daraus Editor-Links und Webhook-URLs. | `http://localhost:5678/` |
| `N8N_PORT` | Port auf dem Docker-Host | `5678` |
| `GENERIC_TIMEZONE` | Zeitzone für Schedule-Trigger und Zeitstempel, z. B. `Europe/Berlin` | `UTC` |
| `N8N_SECURE_COOKIE` | Ob das Login-Cookie das Flag `Secure` bekommt | `true` |

**Zu `N8N_SECURE_COOKIE`:** Browser schicken ein `Secure`-Cookie nur über HTTPS (Ausnahme: `localhost`).
- Rufst du n8n per **reinem HTTP über eine andere Adresse als localhost** auf, z. B. `http://<server>:5678`, setze `N8N_SECURE_COOKIE=false`. Sonst klappt das Login im Editor nicht.
- Steht n8n **hinter HTTPS** (Reverse-Proxy mit TLS), lass den Wert auf `true`.

Beachte dabei: Ohne TLS gehen Passwort und API-Keys im Klartext übers Netz. Für alles außer einem vertrauenswürdigen lokalen Netz gehört ein TLS-Reverse-Proxy vor n8n.

Optional kannst du in `.env` auch `N8N_OWNER_EMAIL=<deine Adresse>` setzen. Das Setup-Skript legt den Owner dann mit dieser Adresse an; sonst nimmt es einen Platzhalter.

## 3. n8n starten

```sh
docker compose up -d
```

Warten, bis n8n bereit ist:

```sh
curl -fsS http://localhost:5678/healthz
```

Die Antwort muss `{"status":"ok"}` sein.

Die Compose-Datei schaltet den **Instanz-MCP** per Env ein (`N8N_MCP_MANAGED_BY_ENV=true`, `N8N_MCP_ACCESS_ENABLED=true`). Er ist damit nach jedem Start an, und der Schalter im Editor ist gesperrt. Neue Workflows, die ein Mensch im Editor anlegt, sind trotzdem **nicht** automatisch für den MCP freigegeben; das bleibt eine Entscheidung im Editor.

Der Container startet mit `restart: unless-stopped` bei einem Neustart des Docker-Hosts wieder mit.

## 4. `setup_owner.sh` ausführen

```sh
sh setup_owner.sh
```

Das Skript arbeitet Schritt für Schritt und ist wiederholbar. Jeder Schritt prüft, ob sein Ergebnis schon da ist.

1. **Owner anlegen:** erzeugt ein zufälliges Passwort, legt den Owner-Account an und meldet sich an.
2. **Public-API-Key erzeugen** mit **minimalen, nur lesenden Scopes**: `workflow:read`, `workflow:list`, `execution:read`, `execution:list`. Mehr braucht ScarabHive nicht; geschrieben wird ausschließlich über den MCP.
3. **Instanz-MCP prüfen:** Er muss an sein (siehe Schritt 3). Ist er aus, bricht das Skript mit einem Hinweis auf die Compose-Variablen ab.
4. **MCP-Key erzeugen:** rotiert den MCP-Key des Owners und speichert den neuen Schlüssel. n8n zeigt ihn nur in genau dieser Antwort im Klartext, danach nur noch maskiert.

Alles landet in der Datei `CREDENTIALS` mit den Rechten 0600 (nur dein Benutzer darf lesen):

```
N8N_URL=…
N8N_OWNER_EMAIL=…
N8N_OWNER_PASSWORD=…
N8N_API_KEY=…
N8N_MCP_KEY=…
```

Das Skript gibt keinen Schlüssel und kein Passwort auf der Konsole aus, nur HTTP-Statuscodes.

## 5. Werte in ScarabHive eintragen

In die Datei `config/secrets.env` deiner ScarabHive-Installation gehören genau drei Werte:

```
N8N_BASE_URL=http://localhost:5678
N8N_API_KEY=<N8N_API_KEY aus CREDENTIALS>
N8N_MCP_KEY=<N8N_MCP_KEY aus CREDENTIALS>
```

- `N8N_BASE_URL` ist die Adresse, unter der **ScarabHive** n8n erreicht, ohne abschließenden `/`. Das kann eine andere sein als `N8N_PUBLIC_URL`, z. B. wenn beide auf demselben Host laufen.
- Ist sie eine andere, gehört auch `N8N_PUBLIC_URL=<öffentliche Adresse>` in `config/secrets.env`. Editor-Links und die Webhook-URLs, die der Agent nach dem Veröffentlichen nennt, baut das Plugin daraus; ohne sie aus `N8N_BASE_URL`, und die taugt dann nicht für Aufrufer von außen.
- Das **Owner-Passwort gehört nicht** in `config/secrets.env`. ScarabHive braucht es zur Laufzeit nicht; es bleibt in `CREDENTIALS` für dich.

Danach ScarabHive neu starten. Die Plugin-Instanz ist bereits eingeschaltet (`enabled: true` in `agents/n8n.yaml`) und bietet ohne die Werte keine Tools an. Ohne `N8N_MCP_KEY` stellt das Plugin keine Tools bereit; ohne `N8N_API_KEY` nur die lesenden Knotenwissen-Tools.

`CREDENTIALS` selbst kannst du nach dem Übertragen an einem sicheren Ort ablegen. Liegt sie im Deploy-Verzeichnis, ist sie git-ignored.

## Keys rotieren

Rotiere einen Key, wenn er irgendwo gelandet sein könnte, wo er nicht hingehört, oder regelmäßig nach deiner eigenen Richtlinie.

**MCP-Key:**
1. Die Zeile `N8N_MCP_KEY=…` aus `CREDENTIALS` löschen.
2. `sh setup_owner.sh` erneut ausführen. Das Skript rotiert den Key; der alte ist danach nicht mehr gültig.
3. Den neuen Wert in `config/secrets.env` eintragen und ScarabHive neu starten.

Solange ScarabHive noch den alten Key hat, schlagen die n8n-Tools mit einem Hinweis auf `N8N_MCP_KEY` fehl.

**Public-API-Key:**
1. Die Zeile `N8N_API_KEY=…` aus `CREDENTIALS` löschen.
2. `sh setup_owner.sh` erneut ausführen. Es erzeugt einen neuen Key mit denselben minimalen Scopes.
3. Den neuen Wert in `config/secrets.env` eintragen und ScarabHive neu starten.
4. Den **alten** Key im n8n-Editor in den Einstellungen unter „n8n API“ löschen. Das Skript löscht ihn nicht; ein neuer Key ersetzt den alten nicht automatisch.

**Owner-Passwort:** im n8n-Editor in den persönlichen Einstellungen ändern und den neuen Wert in `CREDENTIALS` nachtragen. Das Setup-Skript braucht es für spätere Rotationen.

**`N8N_ENCRYPTION_KEY`** wird nicht rotiert (siehe Schritt 2).

## Bestehende Instanz

Hast du n8n schon mit einer früheren Fassung dieses Skripts eingerichtet, hat dein Public-API-Key vermutlich **alle** Scopes. Dann, in dieser Reihenfolge:
1. Die Compose-Datei auf den aktuellen Stand bringen (MCP-Variablen) und `docker compose up -d` ausführen. Das Skript bricht sonst beim MCP-Schritt ab.
2. Die Zeile `N8N_API_KEY=…` aus `CREDENTIALS` löschen und das Skript erneut ausführen; es erzeugt einen Minimal-Key und den MCP-Key.
3. Den alten Voll-Key im Editor löschen.

## n8n aktualisieren

Die Image-Version ist in `docker-compose.yml` fest eingetragen (`docker.n8n.io/n8nio/n8n:<version>`). So aktualisierst du:

1. **Sichern.** Die Daten liegen im Docker-Volume `n8n_n8n_data` (Projektname `n8n` plus Volume `n8n_data`):
   ```sh
   docker compose stop
   docker run --rm -v n8n_n8n_data:/data -v "$PWD":/backup alpine \
     tar czf /backup/n8n_data_backup.tgz -C /data .
   ```
   Die Sicherung enthält die Datenbank mit verschlüsselten Credentials. Behandle sie wie `CREDENTIALS`.
2. **Version ändern:** in `docker-compose.yml` die Versionsnummer im `image`-Eintrag ersetzen. Vorher die Release Notes von n8n auf Breaking Changes lesen.
3. **Starten:**
   ```sh
   docker compose pull
   docker compose up -d
   curl -fsS http://localhost:5678/healthz
   ```
4. **Plugin-Konfiguration:** ScarabHive prüft die n8n-Version zur Laufzeit nicht; ohne Owner-Login gibt n8n sie nicht heraus (M-MCP-51). `tested_n8n_version` in `agents/n8n.yaml` ist ein Vermerk: erst anheben, wenn die Prüfungen unten durch sind.

### Was danach in `n8n_facts.md` neu zu prüfen ist

Das Plugin stützt sich auf gemessenes Verhalten einer bestimmten n8n-Version. Nach einem Upgrade kann jedes davon kippen.

**Die Live-Tests des Plugins prüfen diese Punkte:**

| Was | Fakten | Warum es zählt |
|---|---|---|
| Ein ganzer Durchlauf: anlegen, markieren, testen, Ergebnis lesen | M-MCP-4, M-MCP-5, M-MCP-6 | Ergebnisauswertung und Fehlernormalisierung |
| Pins werden befolgt; ungepinnte Knoten laufen live | M-MCP-3, M-MCP-30, M-MCP-39 | Darauf ruht die Sicherheit jedes Testlaufs. |
| Ein gepinnter Sub-Workflow-Aufruf startet den Sub-Workflow nicht | M-MCP-53 | Darum dürfen Sub-Workflows im Test nie live laufen. |
| Code hat Netz über `helpers.httpRequest` | M-MCP-38 | Begründet, warum Code im Test nie live läuft. |
| `validate_workflow` lässt Version 99 und einen leeren Webhook-Pfad durch | M-MCP-H9 | Fängt n8n das inzwischen selbst, fällt die eigene Prüfung weg. |
| Veröffentlichen per `versionId`, Auslösen per Webhook, die laufende Execution finden, Zurücknehmen, Archivieren | M-MCP-65 bis M-MCP-68, M-MCP-70 | Darauf ruhen die Versionsprüfung und das Wiederfinden einer ausgelösten Execution. |

**Von Hand zu prüfen** (mit den Skripten aus den Quellen in `n8n_facts.md`):

| Was | Fakten | Warum es zählt |
|---|---|---|
| Liste und Schemas der MCP-Tools | M-MCP-H4, M-MCP-13, M-MCP-25, M-MCP-27 | Das Plugin leitet Tools weiter; geänderte Namen oder Parameter brechen es. |
| Die übrigen Lücken der Validatoren | M-MCP-H8, M-MCP-8 bis M-MCP-11, M-MCP-32, M-MCP-44 | wie oben |
| Freigabe-Tor pro Workflow | M-MCP-20, M-MCP-21 | zweiter Riegel des Plugins |
| MCP-Env-Variablen | M-MCP-22, M-MCP-23 | Ohne sie ist der MCP nach dem Start aus. |
| Rate-Limit; der MCP vergibt keine Session-ID | M-MCP-24, M-MCP-54 | Budget des Plugins |
| Scopes der Public API | F-AUTH6, F-AUTH7, F-AUTH8 | Der Minimal-Key muss weiter reichen. |
| Speichereinstellungen | M-MCP-41, M-MCP-55 | Testnachweis |
| Agent-Tool-Varianten und versteckte Knoten, MCP-Client-Knoten | M-MCP-42, M-MCP-59, F-NOD11 | Sperr- und Prüfliste des Plugins |

### Live-Tests ausführen

Die Tests legen Workflows mit dem Präfix `zz-probe-live-` an und löschen sie am Ende wieder. Der Laufzeit-Key darf nicht löschen, deshalb brauchen sie einen **zweiten Key**:

1. Im n8n-Editor unter Einstellungen → „n8n API“ einen Key anlegen, mindestens mit `workflow:read`, `workflow:list` und `workflow:delete`. Bietet der Editor keine Auswahl der Scopes, hat der Key alle Rechte: dann nur für den Testlauf anlegen und danach löschen.
2. Diesen Key **nie** in `config/secrets.env` eintragen; er gehört nur in die Umgebung des Testlaufs.
3. Im ScarabHive-Verzeichnis, mit dem Python der ScarabHive-Umgebung:
   ```sh
   N8N_LIVE=1 N8N_BASE_URL=http://<host>:5678 N8N_API_KEY=<Laufzeit-Key> \
   N8N_MCP_KEY=<MCP-Key> N8N_TEST_API_KEY=<zweiter Key> \
     python -m pytest src/plugins/n8n/tests/test_plugin_n8n_live.py -q
   ```
   Ohne `N8N_LIVE=1` oder eine der Variablen werden die Tests übersprungen, nicht bestanden.

Weicht ein Ergebnis ab: den Fakt in `n8n_facts.md` mit neuer Messung korrigieren, dann Design und Plugin anpassen. Erst danach `tested_n8n_version` anheben.
