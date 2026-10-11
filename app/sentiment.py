"""Regelbasierte Tonalität (positiv / neutral / negativ) aus Sicht des VW-Konzerns.

Positiv  = für den Konzern grundsätzlich vorteilhaft bzw. nicht von Nachteil: neue Produkte,
           Verbesserungen, Kundenzuspruch, gute Testergebnisse, gute Marktentwicklung, bessere
           Finanzergebnisse, Dinge, die das Ansehen stärken.
Negativ  = Kritik, Rückrufe, durchschnittliche oder unterdurchschnittliche Testergebnisse,
           rückläufige Absatz-/Umsatz-/Margenzahlen, Verzögerungen, Dinge, die dem Ansehen schaden.
Neutral  = keine klare Tendenz (keine Signale, zu knappe Bilanz oder stark gemischte Signale).

Vorgehen:
1. Text normalisieren (Kleinschreibung, Umlaute ohne Punkte, ß -> ss).
2. Kennzahl + Richtung erkennen ("Absatz sinkt" negativ, "Verlust sinkt" positiv) und diese
   Stellen ausblenden, damit Einzelwörter darin nicht doppelt zählen.
3. Regeln je Kategorie anwenden; Verneinung davor ("kein", "nicht" …) kehrt das Signal um,
   "trotz" davor schwächt es ab.
4. Titel zählt doppelt. Aus der Summe positiver und negativer Signale ergibt sich die Klasse.
Die Regeln stehen unten als Tabelle und werden in der App-Dokumentation (/doku) angezeigt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import unicodedata

from .textutil import fold


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    """Wie fold(), merkt sich aber zu jedem Zeichen die Position im Original."""
    out, pos = [], []
    for i, ch in enumerate(text):
        f = unicodedata.normalize("NFKD", ch.lower().replace("ß", "ss"))
        f = "".join(c for c in f if not unicodedata.combining(c))
        out.append(f)
        pos.extend([i] * len(f))
    return "".join(out), pos

POS, NEU, NEG = "positiv", "neutral", "negativ"
LABELS = (POS, NEU, NEG)


@dataclass(frozen=True)
class Rule:
    category: str
    pattern: str   # regulärer Ausdruck auf normalisiertem Text (klein, ä->a, ö->o, ü->u, ß->ss)
    weight: float  # > 0 positiv, < 0 negativ
    example: str   # lesbares Beispiel für die Dokumentation


# ---------------------------------------------------------------- Kennzahl + Richtung

GOOD_METRICS = (r"absatz|absatze|umsatz|umsatze|gewinn|gewinne|ergebnis|betriebsergebnis|nettogewinn|"
                r"operatives ergebnis|marge|rendite|auslieferungen|auslieferung|verkaufe|verkaufszahlen|"
                r"zulassungen|neuzulassungen|marktanteil|marktanteile|auftragseingang|auftragseingange|"
                r"bestellungen|nachfrage|aktie|aktien|aktienkurs|borsenwert|dividende|cashflow|"
                r"produktion|reichweite")
BAD_METRICS = r"verlust|verluste|kosten|schulden|verschuldung|lagerbestand|lagerbestande|emissionen"
UP = (r"steigt|steigen|stieg|stiegen|gestiegen|steigert|gesteigert|wachst|wachsen|wuchs|gewachsen|"
      r"legt zu|legen zu|legte zu|zugelegt|klettert|klettern|erhoht|hoher|verbessert|verdoppelt|"
      r"springt|zieht an|ziehen an|auf rekordniveau|auf rekordhoch|im plus|deutlich mehr")
DOWN = (r"sinkt|sinken|sank|gesunken|fallt|fallen|fiel|gefallen|bricht ein|brechen ein|brach ein|"
        r"eingebrochen|geht zuruck|gehen zuruck|ging zuruck|zuruckgegangen|schrumpft|schrumpfen|"
        r"geschrumpft|rucklaufig|niedriger|halbiert|verfehlt|sackt ab|abgesackt|stagniert|"
        r"rutscht|rutschen|abgerutscht|schwacher|im minus|deutlich weniger|weniger")

_GAP = r"(?:[ ,]+[a-z0-9.%-]+){0,5}?[ ,]+"
_PAIR_PATTERNS = [
    # (Muster, Vorzeichen der Kennzahl, Richtung)
    (re.compile(rf"(?<![a-z])(?:{GOOD_METRICS})(?![a-z]){_GAP}(?:{UP})(?![a-z])"), +1),
    (re.compile(rf"(?<![a-z])(?:{GOOD_METRICS})(?![a-z]){_GAP}(?:{DOWN})(?![a-z])"), -1),
    (re.compile(rf"(?<![a-z])(?:{BAD_METRICS})(?![a-z]){_GAP}(?:{UP})(?![a-z])"), -1),
    (re.compile(rf"(?<![a-z])(?:{BAD_METRICS})(?![a-z]){_GAP}(?:{DOWN})(?![a-z])"), +1),
    (re.compile(rf"(?<![a-z])(?:{UP})(?![a-z])(?:[ ,]+[a-z0-9.%-]+){{0,3}}?[ ,]+(?:{GOOD_METRICS})(?![a-z])"), +1),
    (re.compile(rf"(?<![a-z])(?:{DOWN})(?![a-z])(?:[ ,]+[a-z0-9.%-]+){{0,3}}?[ ,]+(?:{GOOD_METRICS})(?![a-z])"), -1),
]
PAIR_WEIGHT = 3.0

# ---------------------------------------------------------------- Regeln je Kategorie

_M = r"(?:absatz|umsatz|gewinn|ergebnis|auslieferungs?|verkaufs?|zulassungs?|auftrags?|nachfrage|marge|kurs)"

RULES: list[Rule] = [
    # ======== POSITIV
    Rule("Neues Produkt", r"weltpremiere|premiere|enthull\w*|prasentier\w*|vorgestellt|stell\w* .{0,40}vor(?![a-z])|"
         r"marktstart|markteinfuhrung|produktionsstart|serienstart|startet (?:den )?(?:verkauf|vorverkauf|produktion|"
         r"serienproduktion)|bestellbar|kommt (?:auf den markt|nach deutschland)|"
         r"neue[nmrs]? (?:modell|generation|elektroauto|e-auto|suv|limousine|plattform|batterie|software)\w*",
         2.0, "„VW stellt neuen ID.2 vor“, „Weltpremiere des Elroq“"),
    Rule("Verbesserung", r"verbessert\w*|verbesserung\w*|optimiert\w*|effizienter|mehr reichweite|schneller laden|"
         r"update (?:bringt|behebt|verbessert)|upgrade|innovation\w*|durchbruch|fortschritt\w*",
         1.5, "„Update verbessert Reichweite“"),
    Rule("Kundenzuspruch", r"beliebt\w*|bestseller|kundenzuspruch|zuspruch|ausverkauft|warteliste\w*|"
         r"meistverkauft\w*|zulassungsstark\w*|marktfuhrer\w*|spitzenreiter|nummer eins|gefragt\w*|"
         r"grossauftrag\w*|rekordauftrag\w*|auftrag(?![a-z])|auftrage(?![a-z])|bestellflut|ansturm|"
         r"hohe nachfrage|nachfrageboom|boom(?![a-z])|boomt",
         2.0, "„ID.7 wird zum Bestseller“, „Großauftrag für MAN“"),
    Rule("Gutes Testergebnis", r"testsieg\w*|vergleichssieg\w*|bestnote\w*|klassenbest\w*|spitzenplatz|"
         r"platz (?:eins|1)(?![0-9])|sehr gut|(?:note|urteil|gesamturteil|testurteil)[ :]+gut(?![a-z])|"
         r"(?:funf|5) sterne|gewinnt (?:den |im )?(?:vergleich|test)\w*|uberzeug\w*|kauftipp|empfehlung",
         2.5, "„Testsieger im ADAC-Vergleich“, „fünf Sterne im Euro NCAP“"),
    Rule("Markt/Finanzen positiv", rf"rekord{_M}\w*|{_M}(?:plus|zuwachs|sprung|anstieg|wachstum|hoch)\w*|"
         r"rekord(?:jahr|quartal|halbjahr)\w*|hebt (?:die )?prognose|prognose (?:angehoben|erhoht)|"
         r"bestatigt (?:die )?prognose|ubertrifft\w*|ubertroffen|besser als erwartet|uber den erwartungen|"
         r"sonderdividende|kurssprung|kursfeuerwerk|erholt\w*|erholung|aufschwung|comeback|trendwende|"
         r"gewinnt marktanteil\w*|zuversicht\w*|optimist\w*|kostensenkung\w*",
         2.5, "„Absatzplus in China“, „Porsche hebt Prognose an“"),
    Rule("Investition/Arbeitsplätze", r"investier\w*|investition\w*|milliardeninvestition\w*|eroffnet\w*|"
         r"neues werk|neue fabrik|ausbau|baut .{0,25}aus(?![a-z])|neue arbeitsplatze|neue jobs|"
         r"schafft .{0,25}(?:arbeitsplatze|jobs|stellen)|beschaftigungssicherung|standortsicherung|"
         r"sichert .{0,25}(?:arbeitsplatze|jobs|standort\w*)|tarifeinigung|einigung|geeinigt",
         1.5, "„VW investiert Milliarden in Salzgitter“, „Tarifeinigung erzielt“"),
    Rule("Ansehen positiv", r"auszeichnung\w*|ausgezeichnet|award\w*|preistrager\w*|gewinnt .{0,20}(?:preis|award)|"
         r"geehrt|gelobt|lobt|vorbild\w*|vorreiter\w*|pionier\w*|top-arbeitgeber|beliebteste\w* arbeitgeber|"
         r"klimaneutral\w*|co2-neutral\w*|nachhaltig\w*",
         2.0, "„Auszeichnung für nachhaltige Produktion“"),
    Rule("Recht/Behörden positiv", r"freispruch|freigesprochen|verfahren eingestellt|klage abgewiesen|"
         r"genehmig\w*|zulassung erteilt|grunes licht|freigabe",
         2.0, "„Gericht weist Klage ab“, „grünes Licht für Werk“"),
    Rule("Kooperation", r"partnerschaft\w*|kooperation\w*|joint venture|allianz",
         1.0, "„Partnerschaft mit Rivian“"),
    # ======== NEGATIV
    Rule("Kritik", r"kritik\w*|kritisier\w*|vorwurf|vorwurfe|wirft .{0,30}vor(?![a-z])|rugt|ruge|tadel\w*|"
         r"beanstand\w*|emport|emporung|protest\w*|boykott\w*|shitstorm|unmut|arger|zoff|streit\w*",
         -2.0, "„Kritik an Software-Problemen“, „Verbraucherschützer rügen VW“"),
    Rule("Rückruf", r"\w*ruckruf\w*|ruft .{0,40}zuruck|rufen .{0,40}zuruck|werkstattaktion\w*|serviceaktion\w*",
         -3.0, "„Rückruf wegen Airbag-Problemen“"),
    Rule("Mäßiges/schlechtes Testergebnis", r"befriedigend|ausreichend|mangelhaft|ungenugend|durchschnittlich\w*|"
         r"mittelmass\w*|mittelfeld|(?:vier|4) sterne|(?:drei|zwei|ein|null|[0-3]) sterne?(?![a-z])|"
         r"testverlierer|letzte\w* platz|fallt .{0,20}durch|durchgefallen|nicht bestanden|"
         r"schneidet .{0,20}(?:schlecht|schwach|mau|mittelmassig|durchschnittlich)\w* ab|abgewertet",
         -2.5, "„Golf im Test nur befriedigend“, „drei Sterne im Crashtest“"),
    Rule("Markt/Finanzen negativ", rf"rekordverlust\w*|{_M}(?:ruckgang|einbruch|minus|delle|flaute|warnung|"
         r"schwache|verfall)\w*|gewinnwarnung|senkt (?:die )?prognose|prognose (?:gesenkt|gekappt)|"
         r"kappt (?:die )?prognose|verfehlt (?:die )?erwartungen|schlechter als erwartet|unter den erwartungen|"
         r"rote zahlen|milliardenverlust\w*|kursrutsch|kurssturz|absturz|abgesturzt|einbusse\w*|"
         r"verliert marktanteil\w*|krise\w*|unter druck|insolvenz\w*|pleite",
         -2.5, "„Absatzeinbruch in China“, „Gewinnwarnung“"),
    Rule("Verzögerung", r"verzoger\w*|verschieb\w*|verschoben|verspat\w*|spater als geplant|lieferverzug|"
         r"in verzug|hinkt .{0,20}hinterher|hangt .{0,20}hinterher|ruckstand",
         -2.5, "„Start des ID.2 verzögert sich“"),
    Rule("Ansehen negativ", r"\w*skandal\w*|affare\w*|betrug\w*|manipul\w*|schummel\w*|abschalteinrichtung\w*|"
         r"dieselgate|korruption\w*|untreue|bestechung\w*|zwangsarbeit|menschenrecht\w*|uigur\w*|xinjiang|"
         r"kinderarbeit|greenwashing|imageschaden|vertrauensverlust",
         -3.0, "„Abgasskandal“, „Vorwürfe wegen Zwangsarbeit“"),
    Rule("Recht/Ermittlungen", r"klage\w*|verklagt|anklage\w*|angeklagt|prozess(?![a-z])|urteil gegen|"
         r"verurteil\w*|strafe\w*|strafzahlung\w*|bussgeld\w*|schaden(?:s)?ersatz\w*|razzia|durchsuch\w*|"
         r"ermittl\w*|staatsanwalt\w*|festgenommen|haftstrafe\w*|haft(?![a-z])|sammelklage\w*|verbot\w*|untersagt",
         -2.5, "„Razzia bei Audi“, „Anklage gegen Ex-Manager“"),
    Rule("Stellenabbau/Werke", r"stellenabbau|jobabbau|arbeitsplatzabbau|abbau von|"
         r"streich\w* .{0,25}(?:stellen|jobs|arbeitsplatze)|entlass\w*|kundigung\w*|kurzarbeit|"
         r"werksschliess\w*|schliess(?!lich)\w* .{0,20}werk\w*|werk\w* .{0,20}(?:schliess(?!lich)\w*|dicht)|"
         r"sparprogramm\w*|sparkurs|sparpaket\w*",
         -2.5, "„VW streicht 35.000 Stellen“, „Werksschließung droht“"),
    Rule("Produktion/Technik-Probleme", r"streik\w*|warnstreik\w*|produktionsstopp\w*|produktionsausfall\w*|"
         r"bandstillstand|stillstand|lieferengpass\w*|chipmangel|teilemangel|engpass\w*|lieferprobleme|"
         r"\w*probleme(?![a-z])|\w*problem(?![a-z])|panne\w*|defekt\w*|mangel(?![a-z])|brand(?![a-z])|brande(?![a-z])|"
         r"feuer|hackerangriff\w*|cyberangriff\w*|datenleck\w*|datenpanne\w*|fehler\w*",
         -2.0, "„Software-Probleme bremsen ID.3“, „Streik legt Werk lahm“"),
    Rule("Personal/Führung", r"rucktritt\w*|tritt .{0,20}zuruck|abgang|abgelost|entmachtet|machtkampf\w*|chaos",
         -1.5, "„Rücktritt des Markenchefs“"),
    Rule("Politik/Handel", r"strafzoll\w*|zolle(?![a-z])|zollstreit|handelsstreit|sanktion\w*|fahrverbot\w*",
         -1.5, "„Strafzölle auf E-Autos“"),
    Rule("Unfall/Sicherheit", r"tote(?![a-z])|toten(?![a-z])|todlich\w*|unfall\w*|verletzt\w*",
         -2.0, "„Tödlicher Unfall mit Autopilot“"),
    Rule("Sorgen/Risiken", r"sorge(?![a-z])|sorgen um|angst|droht|drohen|bedroht|gefahrdet|warnung|warnt|"
         r"enttausch\w*|schwach\w*|verliert|minus(?![a-z])",
         -1.5, "„Sorge um Standorte“, „schwaches Quartal“"),
]

_NEGATORS = {"kein", "keine", "keinen", "keiner", "keinem", "nicht", "ohne", "kaum", "weder", "nie", "niemals",
             # englisch
             "no", "not", "without", "never", "nor"}
_CONCESSIVE = {"trotz", "despite"}
_ZH_NEGATORS = ("不", "未", "没有", "没", "无")
_SENT = re.compile(r"[.!?;。！？；]+(?=\s|$|[^\x00-\x7f])")  # nicht bei „ID.2“ oder „2.000“
_SPLIT = re.compile(r"([.!?;。！？；]+(?=\s|$|[^\x00-\x7f]))")
_WORD = re.compile(r"[a-z0-9]+")
_MAX_PER_RULE = 1   # eine Regel zählt je Titel bzw. Teaser höchstens einmal

# ---------------------------------------------------------------- Englisch (für unübersetzte Meldungen)

EN_GOOD_METRICS = (r"sales|deliveries|revenue|revenues|profit|profits|earnings|operating profit|net income|margin|"
                   r"margins|registrations|market share|orders|demand|shares|stock|share price|dividend|"
                   r"cash flow|output|production")
EN_BAD_METRICS = r"loss|losses|costs|debt|inventory|inventories|emissions"
EN_UP = (r"rise|rises|rose|risen|rising|up|grow|grows|grew|grown|growing|jump|jumps|jumped|surge|surges|surged|"
         r"climb|climbs|climbed|increase|increases|increased|soar|soars|soared|beat|beats|double|doubled|"
         r"hit record|at record")
EN_DOWN = (r"fall|falls|fell|fallen|falling|drop|drops|dropped|down|decline|declines|declined|declining|"
           r"slump|slumps|slumped|plunge|plunges|plunged|slide|slides|slid|shrink|shrinks|shrank|sink|sinks|"
           r"sank|tumble|tumbles|tumbled|miss|misses|missed|halve|halved|lower|weaker")

EN_RULES: list[Rule] = [
    Rule("Neues Produkt", r"unveil\w*|launch\w*|debut\w*|premiere\w*|introduc\w*|reveal\w*|start\w* production|"
         r"(?:new|next-generation) (?:model|car|ev|suv|platform|battery|software)\w*", 2.0, "“VW unveils new ID.2”"),
    Rule("Verbesserung", r"improv\w*|upgrade\w*|breakthrough|innovation\w*|more range|faster charging",
         1.5, "“update improves range”"),
    Rule("Kundenzuspruch", r"best-?sell\w*|popular|strong demand|sold out|waiting list\w*|top-selling|"
         r"market leader|big order|major order|wins? (?:an? )?order", 2.0, "“ID.4 becomes best-seller”"),
    Rule("Gutes Testergebnis", r"five-star|5-star|top safety pick|wins? (?:the )?(?:test|comparison)|"
         r"best in class|car of the year", 2.5, "“five-star Euro NCAP rating”"),
    Rule("Markt/Finanzen positiv", r"record (?:sales|deliveries|profit|revenue|quarter|year)|"
         r"raises? (?:its )?(?:guidance|forecast|outlook)|beats? (?:estimates|expectations|forecasts)|"
         r"better than expected|above expectations|rebound\w*|recover\w*|upbeat|optimis\w*|gains? market share",
         2.5, "“Porsche raises guidance”"),
    Rule("Investition/Arbeitsplätze", r"invest\w*|new plant|new factory|opens? (?:a )?(?:plant|factory)|"
         r"expan\w*|new jobs|creates? jobs|(?:wage|labou?r) (?:deal|agreement)|reach\w* (?:a )?deal|agreement",
         1.5, "“VW to invest $5 billion”"),
    Rule("Ansehen positiv", r"award\w*|honou?r\w*|prais\w*|pioneer\w*|carbon-neutral|sustainab\w*",
         2.0, "“wins sustainability award”"),
    Rule("Recht/Behörden positiv", r"acquit\w*|dismiss\w* (?:the )?(?:lawsuit|case|charges)|approv\w*|green light|"
         r"cleared", 2.0, "“court dismisses lawsuit”"),
    Rule("Kooperation", r"partnership\w*|partners? with|joint venture|alliance|cooperation", 1.0,
         "“partnership with Rivian”"),
    Rule("Kritik", r"critici\w*|criticis\w*|backlash|outcry|protest\w*|boycott\w*|slam\w*|blast\w*|accus\w*|"
         r"allegation\w*", -2.0, "“critics slam software”"),
    Rule("Rückruf", r"recall\w*", -3.0, "“recalls 50,000 cars”"),
    Rule("Mäßiges/schlechtes Testergebnis", r"(?:one|two|three|four|[1-4])-star|fails? (?:the )?(?:test|crash test)|"
         r"poor rating|mediocre|average rating", -2.5, "“three-star rating”"),
    Rule("Markt/Finanzen negativ", r"profit warning|cuts? (?:its )?(?:guidance|forecast|outlook)|"
         r"lowers? (?:its )?(?:guidance|forecast|outlook)|miss\w* (?:estimates|expectations)|worse than expected|"
         r"below expectations|record loss|in the red|crisis|under pressure|insolven\w*|bankrupt\w*|"
         r"loses? market share|slowdown|weak\w*", -2.5, "“profit warning”, “sales slump in China”"),
    Rule("Verzögerung", r"delay\w*|postpon\w*|push\w* back|behind schedule|later than planned",
         -2.5, "“ID.2 launch delayed”"),
    Rule("Ansehen negativ", r"scandal\w*|fraud\w*|cheat\w*|manipulat\w*|defeat device\w*|dieselgate|corruption|"
         r"brib\w*|forced labou?r|human rights|uyghur\w*|xinjiang|greenwashing", -3.0, "“emissions scandal”"),
    Rule("Recht/Ermittlungen", r"lawsuit\w*|sue\w*|sued|charg\w* with|indict\w*|convict\w*|fine\w*|fined|"
         r"penalt\w*|probe\w*|investigat\w*|raid\w*|prosecut\w*|class action|ban(?:s|ned)?(?![a-z])",
         -2.5, "“prosecutors probe Audi”"),
    Rule("Stellenabbau/Werke", r"job cuts?|cuts? (?:\w+ )?jobs|layoff\w*|lay off|plant closure\w*|"
         r"clos\w* (?:a |the )?(?:plant|factory)|cost-cutting|savings plan", -2.5, "“VW to cut 35,000 jobs”"),
    Rule("Produktion/Technik-Probleme", r"strike\w*|walkout\w*|production halt\w*|halts? production|shortage\w*|"
         r"supply (?:problems|issues|disruption\w*)|glitch\w*|problem\w*|issues|defect\w*|fault\w*|fire|"
         r"cyberattack\w*|hack\w*|data leak\w*|bug\w*", -2.0, "“software glitches”, “strike halts plant”"),
    Rule("Personal/Führung", r"resign\w*|steps? down|ousted|power struggle|turmoil|chaos", -1.5,
         "“brand chief resigns”"),
    Rule("Politik/Handel", r"tariff\w*|trade war|sanction\w*|duties", -1.5, "“EU tariffs on Chinese-made EVs”"),
    Rule("Unfall/Sicherheit", r"dead|death\w*|kill\w*|fatal\w*|crash\w*|injur\w*", -2.0, "“fatal crash”"),
    Rule("Sorgen/Risiken", r"concern\w*|worr\w*|fear\w*|threat\w*|warn\w*|risk\w*|disappoint\w*|struggl\w*|"
         r"loses?|lost", -1.5, "“concerns over plants”"),
]

# ---------------------------------------------------------------- Chinesisch (ohne Wortgrenzen)

ZH_GOOD_METRICS = r"销量|交付量|交付|营收|收入|利润|净利润|营业利润|利润率|市场份额|订单|需求|股价|股息|产量"
ZH_BAD_METRICS = r"亏损|成本|债务|库存|排放"
ZH_UP = r"增长|上涨|上升|提升|增加|大涨|飙升|翻番|创新高|同比增|环比增"
ZH_DOWN = r"下滑|下降|下跌|减少|大跌|暴跌|萎缩|腰斩|不及预期|同比降|环比降|同比下"

ZH_RULES: list[Rule] = [
    Rule("Neues Produkt", r"发布|首发|亮相|上市|推出|新车|量产|投产|预售", 2.0, "„发布新车“ (stellt neues Auto vor)"),
    Rule("Kundenzuspruch", r"热销|畅销|销冠|供不应求|订单火爆|大单", 2.0, "„热销“ (verkauft sich gut)"),
    Rule("Gutes Testergebnis", r"五星|满分|最高评级", 2.5, "„五星“ (fünf Sterne)"),
    Rule("Markt/Finanzen positiv", r"创纪录|新高|超预期|上调.{0,6}(?:预期|指引)|回暖|复苏", 2.5, "„超预期“ (über den Erwartungen)"),
    Rule("Investition/Arbeitsplätze", r"投资|新工厂|扩产|建厂|增资", 1.5, "„投资“ (Investition)"),
    Rule("Kooperation", r"合作|合资|联手|战略伙伴", 1.0, "„合作“ (Kooperation)"),
    Rule("Ansehen positiv", r"获奖|荣获|殊荣", 2.0, "„荣获“ (ausgezeichnet)"),
    Rule("Kritik", r"批评|质疑|抵制|投诉|维权", -2.0, "„投诉“ (Beschwerden)"),
    Rule("Rückruf", r"召回", -3.0, "„召回“ (Rückruf)"),
    Rule("Mäßiges/schlechtes Testergebnis", r"[一二三四1-4]星|不合格", -2.5, "„三星“ (drei Sterne)"),
    Rule("Markt/Finanzen negativ", r"下调.{0,6}(?:预期|指引)|不及预期|危机|承压|破产|失去.{0,4}份额|份额.{0,4}(?:下降|萎缩)",
         -2.5, "„不及预期“ (unter den Erwartungen)"),
    Rule("Verzögerung", r"推迟|延期|延迟|跳票", -2.5, "„推迟“ (verschoben)"),
    Rule("Ansehen negativ", r"丑闻|造假|作弊|排放门|腐败|贿赂|强迫劳动|新疆", -3.0, "„排放门“ (Abgasskandal)"),
    Rule("Recht/Ermittlungen", r"起诉|诉讼|罚款|处罚|调查|判决", -2.5, "„调查“ (Ermittlungen)"),
    Rule("Stellenabbau/Werke", r"裁员|关厂|关闭.{0,4}工厂|减产|停产", -2.5, "„裁员“ (Stellenabbau)"),
    Rule("Produktion/Technik-Probleme", r"罢工|故障|缺陷|问题|缺芯|断供|起火|自燃", -2.0, "„故障“ (Defekt)"),
    Rule("Politik/Handel", r"关税|制裁|贸易战", -1.5, "„关税“ (Zölle)"),
    Rule("Sorgen/Risiken", r"担忧|风险|警告|失利", -1.5, "„担忧“ (Sorge)"),
]


def _pairs(good: str, bad: str, up: str, down: str, b: str = r"(?<![a-z])", e: str = r"(?![a-z])",
           gap: str = r"(?:[ ,]+[a-z0-9.%-]+){0,5}?[ ,]+", gap2: str = r"(?:[ ,]+[a-z0-9.%-]+){0,3}?[ ,]+"):
    return [
        (re.compile(rf"{b}(?:{good}){e}{gap}(?:{up}){e}"), +1),
        (re.compile(rf"{b}(?:{good}){e}{gap}(?:{down}){e}"), -1),
        (re.compile(rf"{b}(?:{bad}){e}{gap}(?:{up}){e}"), -1),
        (re.compile(rf"{b}(?:{bad}){e}{gap}(?:{down}){e}"), +1),
        (re.compile(rf"{b}(?:{up}){e}{gap2}(?:{good}){e}"), +1),
        (re.compile(rf"{b}(?:{down}){e}{gap2}(?:{good}){e}"), -1),
    ]


_LANG_PAIRS = {
    "de": _PAIR_PATTERNS,
    "en": _pairs(EN_GOOD_METRICS, EN_BAD_METRICS, EN_UP, EN_DOWN),
    "zh": _pairs(ZH_GOOD_METRICS, ZH_BAD_METRICS, ZH_UP, ZH_DOWN, b="", e="", gap=r"[^。，,；]{0,8}?",
                 gap2=r"[^。，,；]{0,4}?"),
}
_LANG_RULES = {
    "de": [(r, re.compile(r"(?<![a-z0-9])(?:" + r.pattern + r")")) for r in RULES],
    "en": [(r, re.compile(r"(?<![a-z0-9])(?:" + r.pattern + r")(?![a-z])")) for r in EN_RULES],
    "zh": [(r, re.compile(r.pattern)) for r in ZH_RULES],
}
_compiled_rules = _LANG_RULES["de"]


def _extra_rules() -> list:
    """Zusätzliche Begriffe aus der Einstellung „Eigene Bewertungsbegriffe“ (begriff = gewicht)."""
    try:
        from . import config
        lines = config.get("lexicon_extra")
    except Exception:  # außerhalb der App (z. B. in Unit-Tests ohne Datenbank)
        return []
    out = []
    for line in lines:
        term, _, w = line.partition("=")
        term = fold(term.strip())
        try:
            weight = float(w.strip().replace(",", "."))
        except ValueError:
            continue
        if term and weight:
            rule = Rule("Eigener Begriff", re.escape(term), weight, term)
            out.append((rule, re.compile(r"(?<![a-z0-9])" + re.escape(term))))
    return out


@dataclass
class Result:
    pos: float = 0.0
    neg: float = 0.0
    hits: list = field(default_factory=list)   # [{"k": Kategorie, "t": Fundstelle, "w": Gewicht}]

    @property
    def score(self) -> float:
        return round(self.pos - self.neg, 2)

    def add(self, other: "Result") -> None:
        self.pos += other.pos
        self.neg += other.neg
        self.hits += other.hits

    def detail(self, limit: int = 8) -> dict:
        # gleiche Fundstellen (z. B. aus mehreren Meldungen) zusammenfassen
        merged: dict[tuple, dict] = {}
        for h in self.hits:
            key = (h["k"], h["t"].lower())
            if key in merged:
                merged[key]["w"] = round(merged[key]["w"] + h["w"], 2)
                merged[key]["n"] = merged[key].get("n", 1) + h.get("n", 1)
            else:
                merged[key] = dict(h)
        top = sorted(merged.values(), key=lambda h: -abs(h["w"]))[:limit]
        return {"pos": round(self.pos, 2), "neg": round(self.neg, 2), "hits": top}


def _modifier(text: str, start: int) -> float:
    if any(n in text[max(0, start - 3):start] for n in _ZH_NEGATORS):
        return -0.5
    before = _WORD.findall(text[max(0, start - 40):start])[-3:]
    if any(w in _NEGATORS for w in before):
        return -0.5
    if any(w in _CONCESSIVE for w in before[-2:]):
        return 0.25
    return 1.0


def _score_segment(original: str, factor: float, rules, pairs=_PAIR_PATTERNS) -> Result:
    res = Result()
    text, posmap = _fold_with_map(original)

    def hit(category: str, offset: int, m, weight: float):
        w = round(weight * factor, 2)
        if w > 0:
            res.pos += w
        else:
            res.neg += -w
        a, b = posmap[offset + m.start()], posmap[offset + m.end() - 1] + 1   # Fundstelle im Originaltext
        res.hits.append({"k": category, "t": original[a:b].strip()[:60], "w": w})

    offset = 0
    for part in _SPLIT.split(text):
        if not part or _SENT.fullmatch(part):
            offset += len(part)
            continue
        s = part
        # 1. Kennzahl + Richtung; gefundene Stellen ausblenden
        for pat, sign in pairs:
            for m in list(pat.finditer(s)):
                hit("Kennzahl + Richtung", offset, m, sign * PAIR_WEIGHT * _modifier(s, m.start()))
            s = pat.sub(lambda m: " " * len(m.group(0)), s)
        # 2. Regeln je Kategorie
        for rule, rx in rules:
            n = 0
            for m in rx.finditer(s):
                hit(rule.category, offset, m, rule.weight * _modifier(s, m.start()))
                n += 1
                if n >= _MAX_PER_RULE:
                    break
        offset += len(part)
    return res


def analyze(title: str, summary: str = "", lang: str = "de") -> Result:
    """lang: Sprache des Textes (de/en/zh); übersetzte Meldungen werden als de bewertet."""
    lang = lang if lang in _LANG_RULES else "de"
    rules = _LANG_RULES[lang] + _extra_rules()
    pairs = _LANG_PAIRS[lang]
    res = _score_segment(title or "", 2.0, rules, pairs)
    res.add(_score_segment(summary or "", 1.0, rules, pairs))
    return res


def fingerprint() -> str:
    """Kennung des Regelstands; ändert sie sich (neues Image), wird beim Start neu bewertet."""
    import hashlib
    parts = [f"{r.category}|{r.pattern}|{r.weight}" for r in RULES + EN_RULES + ZH_RULES]
    parts += [p.pattern for pairs in _LANG_PAIRS.values() for p, _ in pairs]
    parts += [str(PAIR_WEIGHT), str(_MAX_PER_RULE), ",".join(sorted(_NEGATORS))]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def score(title: str, summary: str = "") -> float:
    """> 0 eher positiv, < 0 eher negativ (Kompatibilität)."""
    return analyze(title, summary).score


def classify(pos: float, neg: float, margin: float = 1.5, mixed_ratio: float = 0.75) -> str:
    """Klasse aus positiver und negativer Signalsumme."""
    if pos + neg < 0.5:
        return NEU
    if abs(pos - neg) < margin:
        return NEU
    if min(pos, neg) > 0 and min(pos, neg) >= mixed_ratio * max(pos, neg):
        return NEU
    return POS if pos > neg else NEG


def label_for(result: Result) -> str:
    try:
        from . import config
        margin = float(config.get("sentiment_neutral_margin"))
        mixed = float(config.get("sentiment_mixed_ratio"))
    except Exception:
        margin, mixed = 1.5, 0.75
    return classify(result.pos, result.neg, margin, mixed)


def label(value: float) -> str:
    """Klasse für einen reinen Saldo (ohne Aufschlüsselung)."""
    return classify(max(value, 0), max(-value, 0))
