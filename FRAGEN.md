# Fragenkatalog zur Verbesserung des VW-Nachrichtenmonitors

So nutzen Sie diese Datei: Antworten Sie direkt hinter **Antwort:**. Kreuzen Sie bei Auswahlfragen
mit `[x]` an. Lassen Sie Fragen leer, wenn die aktuelle Annahme passt. Die aktuelle Annahme steht
jeweils als *Annahme*. Die wichtigsten Fragen stehen in Abschnitt A.

---

## A. Die wichtigsten Fragen

**A1. Wofür werden die Ergebnisse genutzt?** (z. B. Reputationsmonitoring, Investmententscheidung,
Forschung, Lagebild für Vorstand/Betriebsrat, persönliches Interesse). Davon hängt ab, wie fein
bewertet und wie streng gefiltert werden soll.
Antwort:

**A2. Aus wessen Sicht ist „positiv/negativ“ gemeint?**
*Annahme: aus Sicht des Konzerns (Wirtschaft, Reputation, Recht, Beschäftigung).*
- [ ] Konzern / Unternehmensführung
- [ ] Aktionäre (Kursrelevanz)
- [ ] Beschäftigte
- [ ] Kunden
- [ ] Gesellschaft / Umwelt
Antwort:

**A3. Reicht positiv/negativ, oder brauchen Sie mehr?**
*Annahme: zweiwertig, ohne „neutral“; bei Gleichstand „negativ“.*
- [ ] zweiwertig lassen
- [ ] zusätzlich „neutral“
- [ ] Skala (z. B. −2 bis +2) oder Stärke 1–5
Antwort:

**A4. Was ist für Sie „ein Thema“?**
*Annahme: eine konkrete Nachrichtengeschichte über bis zu ~6 Tage. Lange Geschichten (z. B. Dieselskandal)
zerfallen in viele Einzelthemen.*
- [ ] so lassen
- [ ] zusätzlich Dauerthemen/Storylines, die Einzelthemen über Jahre bündeln
- [ ] gröber (eher Wochen-/Monatsthemen)
Antwort:

**A5. Ist ein Anthropic-API-Key verfügbar, und welches Kostenbudget ist in Ordnung?**
*Annahme: optional. Claude Opus 5.5 kostet grob 0,5–1 Cent je neuer Meldung; Haiku 5.5 rund 40× weniger.*
- [ ] kein KI-Einsatz
- [ ] KI im laufenden Betrieb (wenige Euro/Monat)
- [ ] zusätzlich einmalig die Historie seit 2000 per KI neu bewerten (geschätzt 20.000–40.000 Meldungen)
Budget pro Monat / einmalig:
Antwort:

**A6. Welche Konzernteile sollen zählen?**
*Annahme: alle Marken und Töchter inkl. Porsche SE, Traton/MAN/Scania, Cariad, PowerCo.*
- [ ] nur die Marke Volkswagen (Pkw)
- [ ] VW-Konzern ohne Porsche SE
- [ ] alles inklusive Porsche SE (aktuell)
- [ ] Sport (VfL Wolfsburg) doch einbeziehen
Antwort:

---

## B. Quellen

**B1. Ist die Quellenliste passend?** Aktuell: tagesschau.de, SPIEGEL, ZEIT, FAZ, Süddeutsche,
Handelsblatt, WirtschaftsWoche, manager magazin, n-tv, Deutschlandfunk, NDR Niedersachsen,
Tagesspiegel, WELT, heise, Automobilwoche. Streichen oder ergänzen?
Antwort:

**B2. Sollen auch diese Quellen dazu?**
- [ ] Regionalpresse am Konzernsitz (Wolfsburger Allgemeine, Braunschweiger Zeitung)
- [ ] Börsen-Zeitung, Finanzportale
- [ ] Fachpresse (Automobilwoche, auto motor und sport, electrive)
- [ ] internationale Medien (Reuters, Bloomberg, FT, NYT – englischsprachig)
- [ ] Pressemitteilungen des Konzerns selbst
- [ ] Social Media / Blogs
Antwort:

**B3. Haben Sie Zugänge zu Bezahlinhalten oder Datenbanken** (z. B. FAZ-Archiv, Spiegel+,
Handelsblatt-Archiv, GENIOS, LexisNexis, dpa)? Damit ließe sich die Historie lückenloser abdecken.
Antwort:

**B4. Sollen alle Medien gleich zählen,** oder sollen Leitmedien beim Gewicht eines Themas stärker zählen?
Antwort:

---

## C. Historie seit 2000

**C1. Wie vollständig muss die Historie sein?** Die freien Archive haben Lücken (z. B. tagesschau.de vor
2010 teilweise depubliziert, RSS-Archive erst ab ca. 2004).
- [ ] repräsentative Stichprobe genügt (Trend über die Jahre)
- [ ] möglichst vollständig (dann ggf. B3 relevant)
Antwort:

**C2. Welche Auswertungen der Entwicklung brauchen Sie?** Aktuell: Anzahl positiver/negativer Themen
je Jahr bzw. Monat.
- [ ] Anteil negativ in Prozent je Jahr
- [ ] nach Marke getrennt
- [ ] nach Kategorie (Finanzen, Recht, Produkt, Personal, Politik …)
- [ ] Vergleich mit Aktienkurs oder Absatzzahlen
- [ ] Liste der „Top-Ereignisse“ je Jahr
Antwort:

**C3. Sollen bekannte Großereignisse als Markierungen im Zeitstrahl erscheinen?** (z. B. VW-Affäre 2005,
Porsche-Übernahmeversuch 2008/09, Dieselskandal 2015, Werksschließungspläne 2024)
Antwort:

---

## D. Bewertung und Themenbildung

**D1. Bitte nach dem ersten Lauf ca. 30 Themen stichprobenartig prüfen:** Wie viele sind falsch bewertet,
wie viele falsch zusammengefasst oder fälschlich getrennt? (Beispiele mit Themen-ID helfen sehr.)
Antwort:

**D2. Gibt es Begriffe, die immer positiv/negativ bzw. nie relevant sein sollen?**
(Die Schlagwortlisten sind im Admin-Bereich editierbar; das Lexikon derzeit nur im Code.)
Antwort:

**D3. Soll das Bewertungslexikon im Admin-Bereich editierbar werden?**
Antwort:

**D4. Sollen bereits erfasste Themen später automatisch umbewertet werden,** wenn neue Meldungen
dazukommen? *Annahme: ja bei regelbasierten Themen; nein bei KI- und manuell bewerteten.*
Antwort:

---

## E. Betrieb und Zugriff

**E1. Wo läuft das System?** (eigener Rechner, Server, NAS, Cloud). Muss es hinter einem Proxy/Firewall
arbeiten? Gibt es einen Reverse-Proxy (HTTPS)?
Antwort:

**E2. Wer soll zugreifen?** *Annahme: Lesen offen, Ändern nur Admin.*
- [ ] nur ich
- [ ] ein Team (mehrere Benutzer, ggf. mit Rollen)
- [ ] Anmeldung über Firmen-Login (SSO)
Antwort:

**E3. Ist ein Scan-Intervall von 4 Stunden richtig?** Oder z. B. stündlich zu Geschäftszeiten?
Antwort:

**E4. Wünschen Sie Benachrichtigungen?**
- [ ] E-Mail-Zusammenfassung täglich/wöchentlich
- [ ] sofort bei neuen negativen Themen
- [ ] Teams / Slack / Telegram
Antwort:

**E5. Wie lange sollen Daten aufbewahrt werden,** und gibt es Vorgaben (Datenschutz, Urheberrecht,
interne Richtlinien)? *Annahme: unbegrenzt; es werden nur Titel, Teaser und Links gespeichert.*
Antwort:

**E6. Wird ein Export in ein anderes System gebraucht?** (aktuell CSV und JSON-API; denkbar: Excel mit
Formatierung, Power BI, wöchentlicher PDF-Report)
Antwort:

---

## F. Sonstiges

**F1. Was fehlt Ihnen nach dem ersten Blick auf die Oberfläche?**
Antwort:

**F2. Was stört?**
Antwort:
