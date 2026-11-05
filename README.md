# CinemaMode – Kinosteuerung für Home Assistant

Benutzerdefinierte Home-Assistant-Integration mit einer eigenen Oberfläche für TV-Steuerung, Streaming-Favoriten und Direktstart-Diagnosen.

**Version 0.2.27 · Beta**

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

## Erste Standarddiagnose

1. Den Fernseher oder Smart-Monitor einschalten.
2. In **Kinosteuerung → Einrichtung → Diagnose öffnen** die lokale TV-IP und das Gerätesystem auswählen.
3. **Verbinden und Standarddiagnose starten** wählen. Eine Samsung-Freigabe gegebenenfalls am Gerät bestätigen.
4. Den gemeinsamen Bericht herunterladen.

Die Standardliste umfasst **Netflix, Apple TV, MagentaTV, RTL+, Plex und YouTube**. Eine zusätzliche App kann optional eingetragen werden. Für diese Diagnose muss kein HA-Raum eingerichtet sein.

Die Diagnose startet keine Filme und installiert keine Software auf dem TV. Sie unterscheidet erkannte Apps, nicht gemeldete Einträge und nicht durchführbare Prüfungen. Eine unvollständige App-Liste beweist nicht, dass eine App fehlt.

## TV-Steuerung und Favoriten

Für die normale Steuerung zunächst den Fernseher beziehungsweise Zuspieler über seine passende Home-Assistant-Integration einrichten. Anschließend in CinemaMode einen Raum und ein Wiedergabeziel zuordnen.

- Vorhandene Gerätefunktionen wie Ein/Aus, Lautstärke, Quellenwahl und Navigation verwenden.
- Filme, Folgen und Videos als Favoriten mit Inhaltslink und optionalem Bild speichern.
- Bilder über Dateiauswahl oder Drag-and-drop hinzufügen.
- Unterstützte App-Starts über eine horizontale App-Auswahl ausführen.
- Unter den erweiterten Diagnoseoptionen zwei Titel in sechs einzeln gestarteten Fällen prüfen und bestätigte lokale Startprofile speichern.

Die tatsächlich angebotenen Funktionen richten sich nach der Geräteanbindung. Einige gerätespezifische Samsung-Startverfahren sowie Plex benötigen zusätzliche private Konfiguration am jeweiligen Home Assistant. Diese Einstellungen und Zugangsdaten werden nicht mitgeliefert.

## Grenzen der Beta

- Direktstarts hängen von Betriebssystem, Firmware, App-Version und Gerätefreigabe ab.
- Eine erreichbare Geräte-API oder ein angenommener Befehl bestätigt noch keine Wiedergabe des gewünschten Titels.
- Die Weiterleitung einer HA-Entität ersetzt keinen direkten Netzwerkzugang zum TV.
- Neue Startverfahren bleiben experimentell, bis das Ergebnis am Gerät bestätigt wurde.
- Es gibt keine allgemeine Zusage für jede TV-Firmware und jede Streaming-App.

## Enthaltene Dateien

Dieses Repository enthält die Integration, ihre statischen Dateien und diese Installationsanleitung. Lokale Konfigurationen, Tokens, SSH-Schlüssel, Diagnoseberichte, Backups und Entwicklungswerkzeuge gehören nicht in das Repository.

Quellen und Lizenzen der App-Logos sind unter [App-Logo-Lizenzen](custom_components/cinema_control/frontend/app-icons/SOURCES.md) sowie in den jeweiligen SVG-Dateien dokumentiert. Marken und Logos gehören ihren jeweiligen Rechteinhabern.
