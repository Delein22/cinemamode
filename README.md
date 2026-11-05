# CinemaMode – Kinosteuerung für Home Assistant

Benutzerdefinierte Home-Assistant-Integration mit einer eigenen Oberfläche für TV-Steuerung, Streaming-Favoriten und Direktstart-Diagnosen.

**Version 0.2.31 · Beta**

## Installation

### Mit HACS

1. In **HACS → Drei-Punkte-Menü → Benutzerdefinierte Repositories** die Adresse `https://github.com/Delein22/cinemamode` mit Typ **Integration** hinzufügen.
2. **CinemaMode – Kinosteuerung** öffnen und herunterladen. Ohne veröffentlichte Releases wird der aktuelle Commit von `main` angeboten.
3. Home Assistant neu starten und die Browserseite vollständig neu laden.
4. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach **Kinosteuerung** suchen und die Einrichtung abschließen.
5. **Kinosteuerung** in der Seitenleiste öffnen.

Wenn HACS noch einen älteren Commit ohne HACS-Metadaten anbietet, die Repository-Informationen in HACS aktualisieren und den neuesten Commit von `main` wählen.

### Manuell

1. Dieses Repository über **Code → Download ZIP** herunterladen und entpacken.
2. Den vollständigen Ordner `custom_components/cinema_control` in den Home-Assistant-Konfigurationsordner kopieren. Das Ergebnis muss so aussehen:

   ```text
   custom_components/
     cinema_control/
       __init__.py
       manifest.json
       frontend/
       translations/
       ...
   ```

3. Home Assistant neu starten.
4. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach **Kinosteuerung** suchen und die Einrichtung abschließen.
5. **Kinosteuerung** in der Seitenleiste öffnen. Falls die Oberfläche nach einer Aktualisierung noch den alten Stand zeigt, den Browser vollständig neu laden.

Getestete Entwicklungsumgebung: Home Assistant **2026.10.4**. Ältere Versionen sind nicht geprüft.

## Direktstart einrichten

1. Den Fernseher einschalten und einen Favoriten mit dem gewünschten Inhaltslink und Abspielverhalten **Fortsetzen** anlegen.
2. In **Kinosteuerung → Einrichtung → Direktstart einrichten** das Wiedergabeziel und den Favoriten wählen.
3. **TV prüfen und Titel testen** starten. Dieser Schritt kann die App und den gewählten Titel am TV öffnen.
4. Falls die Verbindung nicht bestätigt wird, **TV neu verbinden** wählen und gegebenenfalls **Zulassen** am Fernseher bestätigen. Die neue Kopplung wird privat für dieses Ziel gespeichert; die native Samsung-Integration bleibt unverändert.
5. Das tatsächliche Ergebnis melden. Bei einer Profilauswahl Profil und PIN am Fernseher erledigen und danach im Assistenten fortfahren.
6. Erst **Richtiger Titel läuft** speichert einen lokal bestätigten Startweg. Bei einem anderen Ergebnis kann der nächste passende bekannte Weg gezielt getestet werden.

Ein angenommener Befehl oder HTTP-Erfolg bestätigt noch keine Wiedergabe. Der erste Erfolg gilt für **einen Titel und eine Ausgangssituation** auf genau diesem Gerät. Die ausführliche Prüfung mit zwei Titeln und sechs Fällen bleibt zusätzlich verfügbar. Unbekannte Firmware und Apps ohne verfügbaren Startweg können nicht automatisch als kompatibel eingestuft werden.

Bei unklarem Ausgang nach einem gesendeten Befehl startet der Assistent keine weiteren Versuche automatisch. Ein Diagnosebericht lässt sich bei Bedarf herunterladen; Zugangsdaten und rohe Vorschauwerte werden nicht exportiert.

Bei Samsung kann die Anmeldung erfolgreich sein, obwohl die Firmware weder eine App-Liste noch die Home-Vorschau liefert. Die Einrichtung prüft dann unabhängige HTTP-Dienste. Antwortet der Netflix-DIAL-Dienst mit einem passenden App-Status, steht zusätzlich **Netflix · DIAL-Inhaltsaufruf** als experimenteller Titelstartweg bereit. Er benötigt keine Vorschaukachel. Das verwendete Netflix-Titelformat ist im [DIAL-Service von ConnectSDK](https://github.com/ConnectSDK/Connect-SDK-Android-Core/blob/master/src/com/connectsdk/service/DIALService.java) beschrieben; ob die jeweilige Netflix-App es unterstützt, wird am Gerät geprüft. Erst die Bestätigung des tatsächlich laufenden Titels speichert diesen Weg für spätere Favoritenstarts.

## Standarddiagnose ohne Titelstart

1. Den Fernseher oder Smart-Monitor einschalten.
2. In **Kinosteuerung → Einrichtung → Standarddiagnose öffnen** die lokale TV-IP und das Gerätesystem auswählen.
3. **Verbinden und Standarddiagnose starten** wählen. Eine Samsung-Freigabe gegebenenfalls am Gerät bestätigen.
4. Den gemeinsamen Bericht herunterladen.

Die Standardliste umfasst **Netflix, Apple TV, MagentaTV, RTL+, Plex und YouTube**. Eine zusätzliche App kann optional eingetragen werden. Für diese Diagnose muss kein HA-Raum eingerichtet sein.

Die Diagnose startet keine Filme und installiert keine Software auf dem TV. Sie unterscheidet erkannte Apps, nicht gemeldete Einträge und nicht durchführbare Prüfungen. Eine unvollständige App-Liste beweist nicht, dass eine App fehlt.

Zusätzlich zur gemeinsamen App- und Vorschauabfrage liest sie bei Samsung bekannte **REST-Appstatusdienste** sowie die **DIAL-Statusdienste für Netflix und YouTube**. Diese begrenzten HTTP-Abfragen verändern nichts am Fernseher und werden unabhängig von der Vorschau ausgewertet. Erreichbarkeit, App-Status und bestätigte Titelwiedergabe bleiben getrennte Nachweise. Für einen echten Startversuch anschließend **Direktstart einrichten → TV prüfen und Titel testen** mit einem eingerichteten Wiedergabeziel und Favoriten verwenden; eine reine Standarddiagnose kann keinen Startweg bestätigen.

## TV-Steuerung und Favoriten

Für die normale Steuerung zunächst den Fernseher beziehungsweise Zuspieler über seine passende Home-Assistant-Integration einrichten. Anschließend in CinemaMode einen Raum und ein Wiedergabeziel zuordnen.

Läuft CinemaMode auf demselben Home Assistant wie die Fernseher, bleiben die Optionen für eine andere Home-Assistant-Instanz deaktiviert. URL, Zugriffsschlüssel und zusätzliche Entitätskennungen sind dafür nicht nötig. Für TV-Apps den jeweiligen Fernseher auch als Wiedergabegerät des Raums auswählen.

Bei Samsung und anderen Geräten mit Quellenwahl werden die App-Namen aus der aktuellen Quellenliste des ausgewählten Wiedergabegeräts automatisch zugeordnet. Zum Einrichten einer zusätzlichen App-Schaltfläche genügt **Nur App öffnen**; die **App-Zuordnung** bleibt auf **Automatisch erkennen**. Bei mehreren passenden Quellen kann die gewünschte Quelle aus einer Liste gewählt werden. Bereits ausdrücklich eingetragene Zuordnungen bleiben erhalten. Ein ausgeschalteter TV verhindert das Speichern des Raums nicht; die tatsächliche Quelle wird beim App-Aufruf geprüft.

- Vorhandene Gerätefunktionen wie Ein/Aus, Lautstärke, Quellenwahl und Navigation verwenden.
- Filme, Folgen und Videos als Favoriten mit Inhaltslink und optionalem Bild speichern.
- Bilder über Dateiauswahl oder Drag-and-drop hinzufügen.
- Unterstützte App-Starts über eine horizontale App-Auswahl ausführen.
- Unter den erweiterten Diagnoseoptionen zwei Titel in sechs einzeln gestarteten Fällen prüfen und bestätigte lokale Startprofile speichern.

### Netflix direkt auf Samsung-Geräten

Für lokale Samsung-TVs verwendet CinemaMode die bereits vorhandene Samsung-Kopplung. Beim Öffnen eines Netflix-Favoriten liest es die aktuelle Netflix-Vorschau **dieses Geräts** und sendet den Titelaufruf selbst an den TV. Ein eigener `rest_command` oder eine YAML-Änderung ist dafür nicht erforderlich. Wohnzimmer-TV und Monitor verwenden jeweils ihre eigene Adresse, Kopplung und Vorschau.

Dieser Weg setzt voraus, dass die Firmware eine passende Netflix-Preapp-Aktion in der Vorschau liefert. Fehlt das bekannte Format, wird kein Titelbefehl gesendet; die Meldung verweist auf die Diagnose. Ein angenommener Aufruf bestätigt keine Wiedergabe und garantiert keine automatische Profilauswahl. Die neu angebundene Kombination muss am jeweiligen Gerät geprüft werden.

Die alte optionale Netflix-Aktion für eine entfernte HA-Instanz bleibt für vorhandene Testserver-Verbindungen nutzbar. Sie wird beim lokalen Start nicht benötigt; bestehende YAML-Einträge werden nicht automatisch geändert oder entfernt.

Die tatsächlich angebotenen Funktionen richten sich nach der Geräteanbindung. Einige gerätespezifische Samsung-Startverfahren für weitere Dienste sowie Plex benötigen zusätzliche private Konfiguration am jeweiligen Home Assistant. Diese Einstellungen und Zugangsdaten werden nicht mitgeliefert.

## Grenzen der Beta

- Direktstarts hängen von Betriebssystem, Firmware, App-Version und Gerätefreigabe ab.
- Eine erreichbare Geräte-API oder ein angenommener Befehl bestätigt noch keine Wiedergabe des gewünschten Titels.
- Die Weiterleitung einer HA-Entität ersetzt keinen direkten Netzwerkzugang zum TV.
- Neue Startverfahren bleiben experimentell, bis das Ergebnis am Gerät bestätigt wurde.
- Es gibt keine allgemeine Zusage für jede TV-Firmware und jede Streaming-App.

## Enthaltene Dateien

Dieses Repository enthält die Integration, ihre statischen Dateien und diese Installationsanleitung. Lokale Konfigurationen, Tokens, SSH-Schlüssel, Diagnoseberichte, Backups und Entwicklungswerkzeuge gehören nicht in das Repository.

Quellen und Lizenzen der App-Logos sind unter [App-Logo-Lizenzen](custom_components/cinema_control/frontend/app-icons/SOURCES.md) sowie in den jeweiligen SVG-Dateien dokumentiert. Marken und Logos gehören ihren jeweiligen Rechteinhabern.
