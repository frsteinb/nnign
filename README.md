# nnign – No News Is Good News

Nachrichtenmonitor für den VW-Konzern.

> **Hobbyprojekt:** privat, nicht kommerziell, ohne Verbindung zur Volkswagen AG. Die Bewertungen beruhen auf
> offengelegten persönlichen Annahmen (siehe In-App-Dokumentation unter `/doku`) und sind keine Beratung –
> bitte eigene Überlegungen anstellen.

Durchsucht seriöse deutschsprachige Nachrichtenquellen nach Meldungen über den Volkswagen-Konzern,
fasst sie zu **Nachrichtenthemen** zusammen, bewertet jedes Thema als **positiv**, **neutral** oder **negativ**
(mit nachvollziehbarer Begründung) und stellt alles über einen Webserver auf **Port 8000** bereit.

* **Beim ersten Start:** Initial-Scan für den Erfassungszeitraum – standardmäßig **die letzten vier Wochen**.
  Der Admin kann den Zeitraum jederzeit erweitern (z. B. zurück bis 2000); die Historie wird dann im Hintergrund abgerufen.
* **Danach:** alle **4 Stunden** (einstellbar) ein Scan auf *neue* Meldungen.
* Pro Thema gespeichert: Erfassungsdatum, Kurzthema, Abstract, Bewertung, 1–5 Quellen (Links), erstes Datum.
* Kompakte Statistik zum ausgewählten Datenbestand auf der Startseite, ausführliche Dokumentation unter `/doku`
  (inkl. Bewertungskriterien, Annahmen und dem vollständigen KI-Prompt).
* Alles ist im Admin-Bereich unter `/admin` konfigurierbar – auch Erfassungszeitraum, Aktualisierung, Löschen
  und Neu-Initialisierung sowie der KI-Prompt.

## Schnellstart

```bash
docker compose up -d --build
```

Dann <http://localhost:8000> öffnen. Optionale Einstellungen (Port, Admin-Passwort, API-Key, Proxy …)
kommen in eine Datei `.env` neben `docker-compose.yml`; Vorlage: `cp .env.example .env`.
Die Daten liegen in `./data`. Ohne Docker Compose:

```bash
docker build -t nnign .
docker run -d --name nnign -p 8000:8000 -v "$PWD/data:/data" nnign
```

**Admin-Zugang:** Benutzer `admin`. Ist `ADMIN_PASSWORD` nicht gesetzt, wird beim ersten Start ein
Passwort erzeugt und im Log ausgegeben (`docker logs nnign`) sowie in
`data/admin_password.txt` abgelegt. Es kann unter `/admin` geändert werden.

**Optional – KI-Analyse:** Ein Sprachmodell bewertet dann Relevanz, Themenzuordnung, Kurztitel,
Abstract und Tonalität. Ohne Konfiguration arbeitet das System vollständig regelbasiert.

| `LLM_PROVIDER` | Anbindung | Pflichtangaben |
|---|---|---|
| `anthropic` (Standard) | offizielles Anthropic-SDK | `ANTHROPIC_API_KEY` (Modell optional, Standard `claude-opus-5-5`) |
| `openai` | OpenAI-API | `LLM_MODEL`, `OPENAI_API_KEY` |
| `azure` | Azure OpenAI | `LLM_MODEL` (= Deployment), `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` |
| `gemini` | Google Gemini (OpenAI-kompatibler Endpoint) | `LLM_MODEL`, `GEMINI_API_KEY` |
| `mistral` / `groq` / `openrouter` | jeweilige API | `LLM_MODEL`, `MISTRAL_API_KEY` / `GROQ_API_KEY` / `OPENROUTER_API_KEY` |
| `ollama` | lokales Ollama (Standard: auf dem Docker-Host) | `LLM_MODEL`; optional `OLLAMA_HOST` |
| `openai_compatible` | beliebiger OpenAI-kompatibler Server (LM Studio, vLLM, LiteLLM …) | `LLM_BASE_URL`, `LLM_MODEL`, ggf. `LLM_API_KEY` |

Statt der anbieterspezifischen Key-Variablen kann immer `LLM_API_KEY` gesetzt werden, `LLM_BASE_URL`
überschreibt die Standard-Adresse. Alles ist auch im Admin-Bereich einstellbar; „KI-Verbindung testen“
prüft die Konfiguration. Strukturierte JSON-Ausgabe wird genutzt, wo der Anbieter sie unterstützt;
sonst fällt das System automatisch auf einfachere Formate zurück.

```bash
ADMIN_PASSWORD=meinPasswort ANTHROPIC_API_KEY=sk-ant-... docker compose up -d --build
LLM_PROVIDER=ollama LLM_MODEL=llama3.1 docker compose up -d --build
```

## Seiten und Schnittstellen

| Pfad | Inhalt |
|---|---|
| `/` | Themenliste mit Filtern (Suche, Bewertung, Medium, Zeitraum) und Zeitstrahl positiv/neutral/negativ über den Erfassungszeitraum; Klick auf ein Jahr zeigt die Monate |
| `/thema/{id}` | Thema mit allen Meldungen; Bearbeiten, Zusammenführen, Abtrennen, Löschen (Admin) |
| `/doku` | Dokumentation: Ablauf, Quellen, Bewertungskriterien und Annahmen, KI-Anfrage und Prompt, Statistik, Einstellungen |
| `/admin` | Status, Daten & Erfassungszeitraum (ändern, aktualisieren, löschen, neu initialisieren), Einstellungen, Quellen, Protokolle |
| `/api/topics`, `/api/topics/{id}`, `/api/stats`, `/api/timeline`, `/api/status` | JSON-API (gleiche Filterparameter wie `/`) |
| `/export.csv` | CSV-Export (Semikolon, UTF-8, Excel-tauglich) |
| `/docs` | automatisch erzeugte API-Dokumentation |
| `/healthz` | Health-Check |

## Konfiguration

Jede Einstellung hat einen eingebauten Standardwert, kann per Umgebungsvariable `NNIGN_<NAME>`
vorbelegt werden (z. B. `NNIGN_SCAN_INTERVAL_HOURS=2`, `NNIGN_COLLECT_SINCE=2005-01-01`) und im
Admin-Bereich überschrieben werden (der Admin-Wert hat Vorrang). Die wichtigsten:

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `scan_interval_hours` | 4 | Intervall der regulären Scans |
| `collect_since` / `collect_until` | vier Wochen vor dem Erststart / laufend | Erfassungszeitraum (bequemer: Admin → Daten & Erfassungszeitraum) |
| `sentiment_neutral_margin` / `lexicon_extra` | 1,5 / – | Wann „neutral“ vergeben wird; eigene Bewertungsbegriffe |
| `llm_system_prompt` / `llm_instructions` | mitgeliefert | KI-Prompt, im Admin-Bereich editierbar |
| `search_enabled` | an | Nachrichtensuche (Google News RSS mit `site:`) zusätzlich zu den Feeds |
| `keywords_strong` / `keywords_brands` / `keywords_exclude` | siehe UI | Relevanzregeln (reguläre Ausdrücke) |
| `cluster_threshold` / `cluster_window_days` | 0,34 / 6 | Wann Meldungen zum selben Thema zusammengefasst werden |
| `llm_provider` / `llm_model` | anthropic / (Standard des Anbieters) | KI-Anbieter und Modell, auch per `LLM_PROVIDER` / `LLM_MODEL` |
| `llm_for_backfill` | aus | KI auch für die Historie verwenden (viele tausend Aufrufe) |
| `public_dashboard` | an | Übersicht ohne Login sichtbar |

Weitere Umgebungsvariablen: `ADMIN_PASSWORD`, `LLM_*` und die Key-Variablen der Anbieter (siehe oben), `DATA_DIR` (Standard `/data`),
`LOG_LEVEL`.

## Woher die Meldungen kommen

| Weg | Zweck |
|---|---|
| RSS/Atom-Feeds der Medien | aktuelle Meldungen, mit Teaser |
| Nachrichtensuche (`site:domain`, Zeitfenster) | findet auch Artikel, die nicht (mehr) im Feed stehen; im Backfill in 90-Tage-Fenstern für den ganzen Erfassungszeitraum |
| Archivseiten der Medien (Spiegel täglich, tagesschau täglich, ZEIT wöchentlich, heise monatlich) | lückenlose Historie; für Treffer werden Teaser und Datum aus den Meta-Tags der Artikelseite gelesen |
| Internet Archive (Wayback Machine) | archivierte Schnappschüsse der Feeds (monatlich), auch für Medien ohne eigenes Archiv; außerdem Ersatz, wenn eine Archivseite nicht erreichbar ist |

Der Backfill arbeitet **grob nach fein**: zuerst jede 32. Periode über den ganzen Zeitraum, dann
immer dichter. So steht schon nach kurzer Zeit ein Überblick über alle Jahre, der sich laufend
verfeinert. Archivquellen, die dauerhaft nichts liefern (z. B. weil sich eine URL geändert hat),
werden nach 15 Fehlschlägen in Folge automatisch gestoppt und im Admin-Bereich angezeigt.

## Daten

Alles liegt in einer SQLite-Datei unter `./data/nnign.sqlite3` (Volume `/data`). Sichern = Datei
kopieren. Zurücksetzen = Container stoppen, Datei löschen, starten.

## Entwicklung

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest                       # Tests laufen komplett offline gegen einen lokalen Mock-Server
uvicorn app.main:app --reload --port 8000
```

Aufbau: `app/fetcher.py` (Abruf), `app/relevance.py` (Relevanz), `app/sentiment.py` (Lexikon),
`app/topics.py` (Themenbildung, TF-IDF-Kosinus), `app/llm.py` (KI-Anbieter), `app/pipeline.py`
(Scans, Backfill, Zeitsteuerung), `app/main.py` (Web/API/Admin).

Siehe auch [BERICHT.md](BERICHT.md) und [FRAGEN.md](FRAGEN.md).
