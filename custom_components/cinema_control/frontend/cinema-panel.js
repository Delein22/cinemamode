/* Local native web component. No build step, CDN, tracking or bundled framework. */
import {appArtwork} from "./app-catalog.js?v=0.2.29";

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const id = () => crypto.randomUUID().replaceAll("-", "");
const appLabels = {netflix:"Netflix", disney:"Disney+", prime:"Prime Video", apple:"Apple TV+", youtube:"YouTube"};
const labels = {...appLabels, plex:"Plex"};
const diagnosticObservations = {not_tested:"Nicht getestet",app_only:"Nur App-Startseite",profile_selection:"Profilauswahl",title_page:"Titelseite",playing:"Titel läuft",failed:"Fehlgeschlagen"};
const assistantOutcomes = {...diagnosticObservations,playing:"Der richtige Titel läuft",wrong_title:"Ein anderer Titel läuft"};
const assistantScenarios = {app_open:{label:"App geöffnet",instruction:"Öffne die App, wähle dein Profil und lass ihre Startseite offen."},app_closed:{label:"App geschlossen",instruction:"Schließe die App und gehe zum Home-Bildschirm des Fernsehers."},profile_selection:{label:"Profilauswahl",instruction:"Öffne die App bis zur Profilauswahl und lass diese Auswahl stehen."}};
const standardSystems = {samsungtv:"Samsung Tizen",webostv:"LG webOS",androidtv:"Android / Google TV",firetv:"Fire TV",other:"Anderes System"};
const standardApps = ["Netflix","Apple TV","MagentaTV","RTL+","Plex","YouTube"];
const states = {on:"Eingeschaltet", off:"Aus", standby:"Standby", idle:"Bereit", playing:"Wiedergabe gemeldet", paused:"Pause gemeldet", buffering:"Lädt", unknown:"Unbekannt", unavailable:"Nicht erreichbar"};
const active = (job) => job && ["preparing", "dispatching", "awaiting_profile", "observing"].includes(job.phase);
const icon = (name) => {
  const paths = {
    movie:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 10h4m10 0h4M3 14h4m10 0h4"/>',
    menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
    play:'<path d="m8 5 11 7-11 7Z"/>', pause:'<path d="M8 5v14M16 5v14"/>',
    stop:'<rect x="6" y="6" width="12" height="12" rx="1"/>',
    up:'<path d="m6 14 6-6 6 6"/>', down:'<path d="m6 10 6 6 6-6"/>',
    left:'<path d="m14 6-6 6 6 6"/>', right:'<path d="m10 6 6 6-6 6"/>',
    back:'<path d="M9 6 3 12l6 6M3 12h13a5 5 0 0 1 5 5"/>',
    home:'<path d="m3 10 9-7 9 7v11h-7v-7h-4v7H3Z"/>',
    power:'<path d="M12 2v10M6 5a9 9 0 1 0 12 0"/>',
    plus:'<path d="M12 5v14M5 12h14"/>', minus:'<path d="M5 12h14"/>',
    sound:'<path d="M4 9h4l5-5v16l-5-5H4ZM17 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
    edit:'<path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 15Z"/>',
    close:'<path d="m6 6 12 12M6 18 18 6"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.movie}</svg>`;
};

export class CinemaControlPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode:"open"});
    this.shadowRoot.innerHTML = `<link rel="stylesheet" href="/cinema_control_static/cinema-panel.css?v=0.2.29"><div id="screen"></div><dialog aria-labelledby="dialog-title"><div id="form"></div></dialog><div id="notice" role="status" aria-live="polite"></div>`;
    this.view = "favorites";
    this.query = "";
    this.filter = "";
    this.pending = false;
    this.connected = false;
    this.shadowRoot.addEventListener("click", e => this._click(e));
    this.shadowRoot.addEventListener("change", e => this._change(e));
    this.shadowRoot.addEventListener("input", e => this._input(e));
    this.shadowRoot.addEventListener("submit", e => this._submit(e));
    this.shadowRoot.addEventListener("error", event => {
      if (event.target.matches?.("img.poster-image")) event.target.hidden = true;
      if (event.target.matches?.("img.app-brand-image")) {
        event.target.hidden = true;
        event.target.closest(".app-logo")?.classList.add("logo-unavailable");
      }
    }, true);
    this.shadowRoot.querySelector("dialog").addEventListener("close", () => {
      this._cancelImageUpload();
      this._cancelDiagnostic();
      this.returnFocus?.focus();
    });
    try {
      const selected = JSON.parse(localStorage.getItem("cinema-control-selection") || "{}");
      this.roomId = selected.room;
      this.targetId = selected.target;
    } catch { /* A missing or blocked localStorage never blocks HA. */ }
    this._render();
  }

  set hass(value) {
    const previous = this._hass?.connection;
    this._hass = value;
    if (this.isConnected && previous !== value.connection) {
      this._disconnect();
      this._connect();
    }
  }
  get hass() { return this._hass; }
  set narrow(value) { this.toggleAttribute("narrow", value); }
  connectedCallback() { this._connect(); }
  disconnectedCallback() { this._disconnect(); this._cancelImageUpload(); clearTimeout(this.noticeTimer); }

  _disconnect() {
    if (this.diagnostic) this._closeForm();
    this.generation = (this.generation || 0) + 1;
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.connected = false;
  }

  async _connect() {
    if (!this._hass || this.connected || !this.isConnected) return;
    this.connected = true;
    const generation = this.generation = (this.generation || 0) + 1;
    try {
      const unsubscribe = await this._hass.connection.subscribeMessage(data => {
        if (generation !== this.generation) return;
        this.data = data;
        this.error = "";
        this._selectDefaults();
        this._render();
      }, {type:"cinema_control/subscribe"});
      if (generation !== this.generation) unsubscribe();
      else this.unsubscribe = unsubscribe;
    } catch (err) {
      if (generation !== this.generation) return;
      this.connected = false;
      this.error = err.message || "Die Kinosteuerung konnte nicht geladen werden.";
      this._render();
    }
  }

  _selectDefaults() {
    if (!this.data.rooms.some(r => r.id === this.roomId)) this.roomId = this.data.rooms[0]?.id;
    const room = this.room;
    if (!room?.targets.some(t => t.id === this.targetId)) this.targetId = room?.targets[0]?.id;
  }
  get room() { return this.data?.rooms.find(r => r.id === this.roomId); }
  get target() { return this.room?.targets.find(t => t.id === this.targetId); }
  get job() { return this.data?.jobs[this.roomId]; }
  entity(value) { return this.data?.entities.find(e => e.entity_id === value); }
  get blocked() { return this.pending || !this.target?.can_control || (active(this.job) && !["observing","awaiting_profile"].includes(this.job.phase)); }

  _remember() {
    try { localStorage.setItem("cinema-control-selection", JSON.stringify({room:this.roomId,target:this.targetId})); } catch { /* optional */ }
  }

  async _command(operation, args = {}) {
    if (this.pending) return;
    this.pending = true;
    this._render();
    try {
      const result = await this._hass.callWS({type:"cinema_control/command",operation,...args});
      return result;
    } catch (err) {
      throw new Error(err.message || "Die Aktion ist fehlgeschlagen.");
    } finally {
      this.pending = false;
      this._render();
    }
  }

  _notify(message, error = false) {
    const node = this.shadowRoot.querySelector("#notice");
    node.textContent = message;
    node.className = error ? "visible error" : "visible";
    clearTimeout(this.noticeTimer);
    this.noticeTimer = setTimeout(() => { node.className = ""; }, error ? 14000 : 5000);
  }

  _render() {
    const screen = this.shadowRoot.querySelector("#screen");
    const focused = this.shadowRoot.activeElement;
    const focusId = screen.contains(focused) ? focused?.id : null;
    const selection = focused?.selectionStart;
    const railPositions = new Map([...screen.querySelectorAll("[data-rail]")].map(rail => [rail.dataset.rail, rail.scrollLeft]));
    const openDetails = new Set([...screen.querySelectorAll("details[data-state-key][open]")].map(detail => detail.dataset.stateKey));
    const data = this.data;
    const admin = data?.can_configure;
    screen.innerHTML = `<header class="topbar">
      <button class="icon menu" data-op="menu" aria-label="Home-Assistant-Menü öffnen">${icon("menu")}</button>
      <div class="brand">${icon("movie")}<span>Kinosteuerung</span></div>
      <nav aria-label="Kinosteuerung">${[["favorites","Favoriten"],["remote","Fernbedienung"],...(admin ? [["settings","Einrichtung"]] : [])].map(([v,n]) => `<button id="nav-${v}" data-op="view" data-view="${v}" aria-current="${this.view===v ? "page" : "false"}">${n}</button>`).join("")}</nav>
    </header>
    <main>${this.error ? `<section class="panel"><h1>Verbindung fehlgeschlagen</h1><p role="alert">${esc(this.error)}</p><button data-op="reload">Erneut versuchen</button></section>` : !data ? `<section class="panel" aria-busy="true"><h1>Kinosteuerung</h1><p>Geräte und Favoriten werden geladen …</p></section>` : this.view === "settings" ? this._settings() : this._home()}</main>`;
    for (const rail of screen.querySelectorAll("[data-rail]")) rail.scrollLeft = railPositions.get(rail.dataset.rail) || 0;
    for (const detail of screen.querySelectorAll("details[data-state-key]")) detail.open = openDetails.has(detail.dataset.stateKey);
    if (focusId) {
      const replacement = this.shadowRoot.getElementById(focusId);
      replacement?.focus({preventScroll:true});
      if (typeof selection === "number" && replacement?.type === "search") replacement.setSelectionRange(selection, selection);
    }
  }

  _targetSelectors() {
    const room = this.room, target = this.target;
    return `<label>Raum<select id="room-select">${this.data.rooms.map(r => `<option value="${esc(r.id)}" ${r.id===room.id?"selected":""}>${esc(r.name)}</option>`).join("")}</select></label>
      <label>Wiedergabeziel<select id="target-select">${room.targets.map(t=>`<option value="${esc(t.id)}" ${t.id===target?.id?"selected":""}>${esc(t.name)}</option>`).join("")}</select></label>`;
  }

  _home() {
    const room = this.room, target = this.target;
    const tv = this.entity(room?.tv_entity_id);
    return `<div class="title-row"><div><h1>${this.view === "remote" ? "Fernbedienung" : "Filmabend"}</h1><p class="muted">${room ? esc(room.name) + " · " + esc(target?.name) : "Ordne zuerst einen Fernseher und ein Wiedergabeziel zu."}</p></div>${this.data.can_configure && this.view === "favorites" ? `<button data-op="favorite-new">${icon("plus")} Favorit hinzufügen</button>` : ""}</div>
      ${room ? `<section class="routing" aria-label="Wiedergabeziel">${this._targetSelectors()}
      <div class="signal" role="status"><span class="state-dot ${["on","idle","playing","paused","buffering"].includes(tv?.state) ? "is-on" : ""}" aria-hidden="true"></span><div><strong>${esc(states[tv?.state] || "Unbekannt")}</strong>${target?.entity_id !== room.tv_entity_id ? `<span class="small muted">${esc(target?.hdmi_source)} · ${esc(states[this.entity(target?.entity_id)?.state] || "Unbekannt")}</span>` : ""}</div></div></section>` : `<section class="panel empty"><h2>Dein erster Kinoraum</h2><p>Wähle deinen Fernseher aus Home Assistant und richte den Raum ein.</p>${this.data.can_configure ? `<button class="primary" data-op="room-new">Raum einrichten</button>` : `<p>Ein Administrator kann die Gerätezuordnung einrichten.</p>`}</section>`}
      ${this.job ? `<section class="job panel" role="status" aria-live="polite"><div class="section-head"><h2>${this.job.phase === "awaiting_profile" ? "Netflix-Profil bestätigen" : active(this.job) ? "Start läuft" : this.job.phase === "failed" ? "Start fehlgeschlagen" : "Letzter Start"}</h2>${active(this.job) ? `<button data-op="cancel" ${this.pending?"disabled":""}>Abbrechen</button>` : ""}</div><p class="${this.job.phase === "failed" ? "negative" : ""}">${esc(this.job.message)}</p>${this.job.phase === "awaiting_profile" ? `<p class="small muted">Benutze die TV-Fernbedienung oder die zugeordnete Fernbedienung unten. Eine Profil-PIN wird am TV eingegeben. Ohne Bestätigung endet die Vorbereitung nach zwei Minuten.</p>${this.job.target_id === this.targetId ? `<button data-op="confirm-profile" data-job="${esc(this.job.id)}" ${this.pending || !target?.can_control ? "disabled" : ""}>Profil „${esc(this.job.profile_name)}“ ist ausgewählt</button>` : `<p>Wähle oben das zugehörige Wiedergabeziel, um das Profil zu bestätigen.</p>`}` : ""}${this.job.phase === "done" && this.job.prime_continue && this.job.target_id === this.targetId && target?.can_control ? `<p class="small muted">Klicke erst, wenn am TV „Fortsetzen“ oder „Abspielen“ markiert ist.</p><button data-op="prime-continue" data-job="${esc(this.job.id)}" ${this.pending ? "disabled" : ""}>Fortsetzen bestätigen</button>` : ""}<details><summary>Ablauf ansehen</summary><ol>${this.job.steps.map(s=>`<li>${esc(s)}</li>`).join("")}</ol></details></section>` : ""}
      ${room ? `<div class="workspace ${this.view === "remote" ? "remote-view" : ""}">${this.view === "favorites" ? this._tvApps() + this._library() : this._remote()}</div>${this.view === "remote" ? this._appManagement() : ""}` : ""}`;
  }

  _library() {
    const favorites = this.data.favorites.filter(f => (!this.filter || f.provider === this.filter) && f.name.toLocaleLowerCase("de").includes(this.query.toLocaleLowerCase("de")));
    const target = this.target;
    return `<section class="panel library" aria-labelledby="favorites-heading"><div class="section-head"><h2 id="favorites-heading">Deine Favoriten <span class="small muted">${favorites.length}</span></h2>
      <div class="filters"><label class="search-label"><span class="sr-only">Favoriten durchsuchen</span><input id="favorite-search" type="search" placeholder="Titel suchen" value="${esc(this.query)}"></label><label><span class="sr-only">Streamingdienst filtern</span><select id="provider-filter"><option value="">Alle Dienste</option>${Object.entries(labels).map(([key,name]) => `<option value="${key}" ${this.filter===key?"selected":""}>${name}</option>`).join("")}</select></label></div></div>
      ${target?.profile === "samsungtv" && this.data.favorites.some(f => f.provider === "netflix") ? `<details class="profile-prepare" data-state-key="netflix-profile"><summary>Netflix-Profil vorbereiten</summary><div class="now-playing"><p class="small muted">${target.capabilities?.netflix_title ? "Der Favorit öffnet den gewählten Titel. Falls Netflix nachfragt, wähle dein Profil am TV." : "Wähle den Titel nach dem Appstart am TV."}</p><p class="small">Profil ${esc(target.apps?.netflix?.profile_name || "In der Raumeinrichtung festlegen")}</p><button data-op="netflix-prepare" ${this.blocked || active(this.job) ? "disabled" : ""}>Netflix und Profil vorbereiten</button></div></details>` : ""}
      <div class="favorite-list" role="list">${favorites.length ? favorites.map(f => {
        const app = target?.apps[f.provider];
        const directNetflix = f.provider === "netflix" && target?.capabilities?.netflix_title;
        const directDisney = f.provider === "disney" && target?.capabilities?.disney_title;
        const directPrime = f.provider === "prime" && target?.capabilities?.prime_title;
        const directYoutube = f.provider === "youtube" && target?.capabilities?.youtube_title;
        const youtubeViaMain = f.provider === "youtube" && target?.capabilities?.youtube_via_main;
        const youtubeSetup = f.provider === "youtube" && !youtubeViaMain && this.data.can_configure && target?.can_control && target.capabilities?.youtube_pairable;
        const isPlex = f.provider === "plex";
        const directPlex = isPlex && target?.capabilities?.plex_title;
        const directTitle = directNetflix || directDisney || directPrime || directYoutube;
        const restartUnavailable = !isPlex && f.playback === "restart" && (directTitle || !target?.capabilities?.seek_start);
        const possible = isPlex ? directPlex : (directTitle || app?.mode === "link") && !restartUnavailable;
        const busy = this.blocked || active(this.job);
        const reason = restartUnavailable ? "Von Anfang an ist auf diesem Gerät nicht verfügbar" : youtubeViaMain && !directYoutube && !possible ? "YouTube-Startaktion auf der Hauptinstanz fehlt" : youtubeSetup && !possible ? "YouTube in Einrichtung verbinden" : isPlex && !possible ? "Plex für dieses Gerät einrichten" : !possible ? "Titelstart für dieses Gerät einrichten" : "";
        const proof = directPlex ? "Titel an Plex senden" : directYoutube ? "YouTube-Video an den Samsung senden" : directTitle ? "Titel an den Samsung senden" : {untested:"Inhaltslink noch ungetestet",app:"Appstart manuell geprüft",title:"Titelaufruf manuell geprüft",playback:"Wiedergabe manuell geprüft"}[app?.tested] || "Inhaltslink an das Gerät senden";
        const status = reason || (!directTitle && !directPlex && app?.tested === "untested" ? "Noch nicht getestet" : "");
        return `<article class="favorite poster-card" role="listitem">
          <button id="start-${esc(f.id)}" class="poster-button" data-op="start" data-id="${esc(f.id)}" aria-label="${esc(f.name)} · Titel öffnen" aria-describedby="status-${esc(f.id)}" title="${esc(reason || proof)}" ${!possible || busy ? "disabled" : ""}>
            <span class="poster-placeholder" aria-hidden="true">${icon("movie")}<span>${esc(labels[f.provider])}</span></span>
            ${f.image ? `<img class="poster-image" src="${esc(f.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ""}
            <span class="poster-play">${icon("play")}<span>Titel öffnen</span></span>
          </button>
          <div class="favorite-info"><div class="favorite-caption"><h3>${esc(f.name)}</h3>${this.data.can_configure ? `<button class="icon favorite-edit" data-op="favorite-edit" data-id="${esc(f.id)}" aria-label="${esc(f.name)} bearbeiten">${icon("edit")}</button>` : ""}</div>
          <p class="favorite-provider">${esc(labels[f.provider])}</p><p id="status-${esc(f.id)}" class="favorite-status small muted">${esc(status)}</p></div>
        </article>`;
      }).join("") : `<div class="empty"><h3>${this.data.favorites.length ? "Keine passenden Titel" : "Was möchtest du sehen?"}</h3><p>${this.data.favorites.length ? "Passe die Suche oder den Dienst an." : "Speichere einen Film, eine Folge oder ein Video als Favorit."}</p>${this.data.can_configure && !this.data.favorites.length ? `<button data-op="favorite-new">Ersten Favoriten hinzufügen</button>` : ""}</div>`}</div></section>`;
  }

  _tvApps() {
    const room = this.room, target = this.target;
    if (!room || !target) return "";
    const tv = this.entity(room.tv_entity_id);
    const native = target.entity_id === room.tv_entity_id && tv?.platform === "samsungtv";
    const sources = native ? [...new Set((Array.isArray(tv.sources) ? tv.sources : []).filter(source => typeof source === "string" && source.trim() && !/^(TV|HDMI(?:[\s-]*\d+)?)$/i.test(source.trim())))] : [];
    const providerFor = name => appArtwork(name).provider;
    const nativeProviders = new Set(sources.map(providerFor).filter(Boolean));
    const apps = sources.map((name,index) => ({name, ...appArtwork(name), source:name, id:`tv-app-${index}`}));
    for (const [provider,name] of Object.entries(appLabels)) {
      if (target.apps[provider] && target.apps[provider].mode !== "off" && !nativeProviders.has(provider)) apps.push({name,...appArtwork(name),provider,id:`app-${provider}`});
    }
    const featured = ["netflix","disney","prime","youtube","plex","apple"];
    const rank = app => featured.includes(app.provider) ? featured.indexOf(app.provider) : featured.length;
    apps.sort((first, second) => rank(first) - rank(second));
    const nativeReady = target.capabilities?.source && ["on","idle","playing","paused","buffering"].includes(tv?.state);
    const busy = this.blocked || active(this.job);
    return `<section class="apps app-shelf" aria-labelledby="apps-heading"><div class="section-head"><div><h2 id="apps-heading">Apps</h2><p class="small muted">${esc(target.name)}</p></div>
      ${apps.length ? `<div class="rail-controls"><button id="apps-previous" class="icon rail-arrow" data-op="rail-scroll" data-direction="previous" aria-label="Vorherige Apps" aria-controls="app-rail">${icon("left")}</button><button id="apps-next" class="icon rail-arrow" data-op="rail-scroll" data-direction="next" aria-label="Weitere Apps" aria-controls="app-rail">${icon("right")}</button></div>` : ""}</div>
      ${apps.length ? `<div id="app-rail" class="scroll-rail app-rail" data-rail="${esc(room.id)}-${esc(target.id)}-apps" tabindex="0" aria-label="Apps durchblättern">${apps.map(app => `<button id="${app.id}" class="app-tile" data-op="${app.source ? "tv-app" : "app"}" ${app.source ? `data-source="${esc(app.source)}"` : `data-provider="${app.provider}"`} ${busy || (app.source && !nativeReady) ? "disabled" : ""} aria-label="${esc(app.name)} öffnen">
        <span class="app-logo ${app.brand ? `brand-${app.brand}` : "app-monogram"} ${app.fullName ? "app-full-name" : ""} tone-${app.tone}" aria-hidden="true">${app.brand ? `<img class="app-brand-image" src="/cinema_control_static/app-icons/${app.brand}.svg" alt="" loading="lazy" decoding="async">` : ""}<span class="app-initials">${esc(app.initials)}</span></span><span class="app-name">${esc(app.name)}</span></button>`).join("")}</div>
        ${native && !nativeReady ? `<p class="small muted app-hint">Schalte den Fernseher ein, um seine Apps direkt zu öffnen.</p>` : ""}` : `<p class="small muted">Hier erscheinen die vom Fernseher gemeldeten und eingerichteten Apps.</p>`}</section>`;
  }

  _appManagement() {
    const target = this.target;
    if (!this.data.can_configure || !target) return "";
    const supported = target.capabilities?.manage_apps;
    const device = this.entity(target.entity_id);
    const description = supported
      ? "Testfunktion für nachinstallierte Android-Apps im Hauptnutzer. System-Apps werden nicht angeboten."
      : "Für diese Geräteanbindung ist kein Löschbefehl verfügbar. Entferne Apps in der App-Verwaltung des Geräts.";
    const ready = device && ["on", "idle", "playing", "paused", "buffering"].includes(device.state);
    return `<section class="panel"><h2>Installierte Apps verwalten</h2>
      <p class="small muted">${esc(description)}</p>
      ${supported ? `<button id="manage-apps" data-op="manage-apps" ${!ready || this.blocked || active(this.job) ? "disabled" : ""}>App-Liste laden</button>
        ${!ready ? "<p class=\"small muted\">Das Wiedergabegerät muss eingeschaltet sein.</p>" : ""}` : ""}
    </section>`;
  }

  async _loadInstalledApps() {
    const target = this.target;
    const args = {room_id: this.roomId, target_id: this.targetId};
    const name = this.entity(target.entity_id)?.name || target.name;
    const result = await this._command("list_apps", args);
    this.appManagement = {args, name, deviceRef: result.device_ref, packages: result.packages};
    const options = result.packages.map(pkg => `<option value="${esc(pkg)}">${esc(pkg)}</option>`).join("");
    this._openForm(`${this._formHeader("Installierte Apps")}
      <p>Wiedergabegerät <strong>${esc(name)}</strong></p>
      ${options ? `<label>App (Paketname)<select id="uninstall-package"><option value="">App wählen</option>${options}</select></label>`
        : "<p>Keine Drittanbieter-Apps im Hauptnutzer gefunden.</p>"}
      <footer><button data-op="dialog-close">Schließen</button>
      ${options ? '<button data-op="review-uninstall">Entfernen …</button>' : ""}</footer>`);
  }

  _reviewUninstall() {
    const packageId = this.shadowRoot.querySelector("#uninstall-package")?.value;
    if (!this.appManagement.packages.includes(packageId)) throw new Error("Bitte eine App auswählen.");
    this.appManagement.packageId = packageId;
    this._openForm(`${this._formHeader("App deinstallieren")}
      <p><strong>${esc(packageId)}</strong> von <strong>${esc(this.appManagement.name)}</strong> entfernen?</p>
      <p class="small muted">Dabei gehen die App-Daten des Hauptnutzers verloren.</p>
      <footer><button data-op="dialog-close" autofocus>Abbrechen</button>
      <button data-op="confirm-uninstall" class="danger">App deinstallieren</button></footer>`);
    this.shadowRoot.querySelector("[autofocus]")?.focus();
  }

  _remote() {
    const room = this.room, target = this.target, caps = target?.capabilities || {};
    const tv = this.entity(room.tv_entity_id), player = this.entity(target.entity_id);
    const profileActions = ["up","down","left","right","ok","back","home","volume_up","volume_down","mute","turn_off","player_turn_off","stop"];
    const button = (action, title, glyph, extra = "") => {
      const usable = caps[action] && (this.job?.phase !== "awaiting_profile" || profileActions.includes(action));
      return `<button id="control-${action}" class="${extra}" data-op="control" data-action="${action}" aria-label="${title}" title="${usable ? title : title + ' ist derzeit nicht verfügbar'}" ${this.blocked || !usable ? "disabled" : ""}>${glyph ? icon(glyph) : title}</button>`;
    };
    return `<section class="panel remote"><div class="section-head"><h2>Fernbedienung</h2><span class="small muted">${esc(target.name)}</span></div>
      <div class="now-playing"><span class="small muted">Vom Gerät gemeldet</span><strong>${esc(player?.media_title || player?.app_id || states[player?.state] || "Unbekannt")}</strong><p class="small muted">${!player?.media_title ? "Kein Titel gemeldet" : "Geräterückmeldung bestätigt keinen neuen Inhaltsstart."}</p></div>
      <div class="power-row">${button("turn_on", "TV einschalten", "power")}${button("turn_off", "TV ausschalten", "power")}</div>
      ${tv?.platform === "samsungtv" && ["off","standby"].includes(tv.state) && target.can_control && !caps.turn_on ? `<p class="small muted">Einschalten über Netzwerk ist für diesen Fernseher noch nicht eingerichtet.</p>` : ""}
      ${target.entity_id !== room.tv_entity_id ? `<div class="power-row">${button("player_turn_on", "Zuspieler wecken", "power")}${button("player_turn_off", "Zuspieler Standby", "power")}</div>` : ""}
      <div class="dpad" aria-label="Navigation auf dem Wiedergabeziel">${button("up", "Nach oben", "up", "up")}${button("left", "Nach links", "left", "left")}${button("ok", "OK", null, "center")}${button("right", "Nach rechts", "right", "right")}${button("down", "Nach unten", "down", "down")}</div>
      <div class="remote-row">${button("back", "Zurück", "back")}${button("home", "Home", "home")}</div>
      <div class="remote-row">${button("play", "Wiedergabe", "play")}${button("pause", "Pause", "pause")}${button("stop", "Stopp", "stop")}${caps.seek_start ? button("restart_stream", "Von vorne", null) : ""}</div>
      <div class="tv-controls"><h3>Fernseher</h3><div class="remote-row volume">${button("volume_down", "Leiser", "minus")}<span class="volume-value">${typeof tv?.volume_level === "number" ? `${Math.round(tv.volume_level * 100)} %` : "—"}</span>${button("volume_up", "Lauter", "plus")}${button("mute", tv?.is_volume_muted ? "Ton einschalten" : "Stumm", "sound")}</div><label>Eingang oder App<select id="tv-source" ${this.blocked || !caps.source ? "disabled" : ""}><option value="">${esc(tv?.source || "Keine aktuelle Quelle gemeldet")}</option>${(tv?.sources || []).map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join("")}</select></label></div>
      ${this.view === "remote" ? this._tvApps() : ""}
      <p class="small muted">${target.can_control ? "Navigation und Wiedergabe → Zuspieler. Lautstärke und Eingang → Fernseher. Nicht verfügbare Funktionen sind deaktiviert." : "Für diese Geräte fehlen dir Steuerungsrechte."}</p></section>`;
  }

  _youtubeSettings() {
    if (!this.data.can_configure || !this.data.rooms.some(room =>
      room.targets.some(target => target.can_control && (target.capabilities?.youtube_pairable || target.capabilities?.youtube_via_main))
    )) return "";
    const target = this.target;
    const viaMain = target?.can_control && target.capabilities?.youtube_via_main;
    const pairable = target?.can_control && !viaMain && target.capabilities?.youtube_pairable;
    const connected = target?.capabilities?.youtube_title;
    const pairing = this.youtubePairing;
    const status = pairing?.roomId === this.roomId && pairing?.targetId === this.targetId ? pairing?.status : null;
    const message = {
      pending: "Verbindung läuft. Bestätige „Zulassen“ am Fernseher.",
      done: "YouTube ist verbunden. Du kannst jetzt ein Video aus deinen Favoriten öffnen.",
      failed: pairing?.errorMessage || "YouTube konnte nicht verbunden werden. Prüfe, ob der Fernseher eingeschaltet ist, und versuche es erneut.",
    }[status];
    return `<section class="settings-section"><div><h2>YouTube auf Samsung</h2><p class="muted">Hier richtest du den Start von YouTube-Videos aus deinen Favoriten ein.</p></div><div class="panel"><div class="filters">${this._targetSelectors()}</div>
      ${viaMain ? `<p class="small muted">${connected ? "Der YouTube-Start über die Hauptinstanz ist bereit." : "Die YouTube-Startaktion fehlt auf der Hauptinstanz."}</p>` : pairable ? `<p class="small muted">Lass den Fernseher eingeschaltet. Bestätige „Zulassen“ am Fernseher, falls die Abfrage erscheint.</p>
      <button id="pair-youtube" data-op="pair-youtube" ${this.blocked || active(this.job) ? "disabled" : ""}>${status === "pending" ? "Wird verbunden …" : status === "failed" ? "Erneut versuchen" : connected ? "Erneut verbinden" : "YouTube verbinden"}</button>
      <p role="status" class="small ${status === "failed" ? "negative" : "muted"}">${esc(message || (connected ? "YouTube ist verbunden." : ""))}</p>` : `<p class="small muted">Wähle ein unterstütztes Samsung-Wiedergabeziel.</p>`}</div></section>`;
  }

  async _pairYoutube() {
    if (!this.data.can_configure || this.blocked || active(this.job) || this.target?.capabilities?.youtube_via_main || !this.target?.capabilities?.youtube_pairable) return;
    const pairing = this.youtubePairing = {roomId:this.roomId, targetId:this.targetId, status:"pending"};
    try {
      await this._command("pair_youtube", {room_id:pairing.roomId, target_id:pairing.targetId});
      pairing.status = "done";
    } catch (err) {
      pairing.status = "failed";
      pairing.errorMessage = typeof err?.message === "string" ? err.message.trim().slice(0, 300) : "";
    }
    this._render();
  }

  _settings() {
    return `<div class="title-row"><div><h1>Einrichtung</h1><p class="muted">Vorhandene HA-Geräte zuordnen und Startverfahren pro App festlegen.</p></div><button data-op="room-new">${icon("plus")} Raum hinzufügen</button></div>
      <section class="settings-section"><div><h2>Räume und Wiedergabeziele</h2><p class="muted">Der Fernseher regelt Ton und Eingang. Jedes Wiedergabeziel erhält eine eigene Fernbedienung und App-Zuordnung.</p><p class="small muted">Für Fire TV wähle das Profil der ADB-Integration. Samsung Tizen erhält nur Funktionen, die seine vorhandene Integration meldet.</p></div><div class="panel">${this.data.rooms.length ? this.data.rooms.map(r=>`<article class="setting-row"><div><h3>${esc(r.name)}</h3><p class="small muted">${esc(this.entity(r.tv_entity_id)?.name || r.tv_entity_id)}</p><p class="small">${r.targets.map(t=>esc(t.name)+(t.entity_id===r.tv_entity_id?" · nativ":" · "+esc(t.hdmi_source))).join("<br>")}</p></div><button data-op="room-edit" data-id="${esc(r.id)}">Bearbeiten</button></article>`).join("") : `<div class="empty"><h3>Noch kein Raum</h3><p>Die Geräteauswahl verwendet ausschließlich Geräte dieser HA-Instanz.</p><button data-op="room-new">Ersten Raum einrichten</button></div>`}</div></section>
      ${this._youtubeSettings()}
      <section class="settings-section"><div><h2>Direktstart-Assistent</h2><p class="muted">Eine gemeinsame Diagnose für die üblichen TV-Apps.</p></div><div class="panel"><p>TV-Adresse und System auswählen. Der Assistent verbindet den Fernseher und erfasst die Standard-Apps in einem Bericht.</p><p class="small muted">Dabei wird kein Film gestartet. Der Bericht wird nur auf Wunsch heruntergeladen.</p><button data-op="diagnostic-open" ${this.pending ? "disabled" : ""}>Assistent öffnen</button></div></section>
      <section class="settings-section"><div><h2>Verfügbarkeit und Nachweise</h2><p class="muted">Appstart, Titelaufruf und Wiedergabe werden getrennt erfasst.</p></div><div class="panel"><p>Neue App-Zuordnungen sind ungetestet. Trage einen manuellen Nachweis erst ein, wenn du das Ergebnis am Gerät gesehen hast.</p><p class="muted">Ein manueller Nachweis beschreibt die geprüfte Kombination. Beim nächsten Start zeigt der Ablauf weiterhin nur die tatsächlich empfangene Rückmeldung.</p><p class="small muted">Anmeldungen und PINs werden am Fernseher eingegeben. Bei Prime Video kann pro Favorit ein einzelnes Profil-OK aktiviert werden.</p></div></section>`;
  }

  _openForm(html) {
    this._cancelImageUpload();
    this._cancelDiagnostic();
    if (!this.shadowRoot.querySelector("dialog").open) this.returnFocus = this.shadowRoot.activeElement;
    this.shadowRoot.querySelector("#form").innerHTML = html;
    const dialog = this.shadowRoot.querySelector("dialog");
    if (!dialog.open) dialog.showModal();
    dialog.querySelector("input,select,button")?.focus();
  }
  _closeForm() { this._cancelImageUpload(); this._cancelDiagnostic(); this.shadowRoot.querySelector("dialog").close(); }
  _formHeader(title) { return `<div class="section-head"><h2 id="dialog-title">${title}</h2><button type="button" class="icon" data-op="dialog-close" aria-label="Schließen">${icon("close")}</button></div><p id="form-error" role="alert"></p>`; }

  _cancelDiagnostic() { this.diagnostic = null; }

  _diagnosticApps(room, target) {
    const sources = this.entity(target?.entity_id)?.sources;
    const nativeApps = Array.isArray(sources) ? sources.filter(name=>typeof name === "string" && name.trim() && !/^(TV|HDMI(?:[\s-]*\d+)?)$/i.test(name.trim()) && !appArtwork(name).provider) : [];
    return [...Object.entries(labels).map(([value,name])=>({value,name})), ...[...new Set(nativeApps)].map(name=>({value:name,name}))];
  }

  _diagnosticForm() {
    if (!this.data.can_configure || this.pending) return;
    const room = this.room?.targets.length ? this.room : this.data.rooms.find(item=>item.targets.length);
    const target = room?.targets.find(item=>item.id === this.targetId) || room?.targets[0];
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem("cinema-control-standard-diagnostic") || "{}"); } catch { /* Optional local defaults never block diagnostics. */ }
    const host = typeof saved?.host === "string" && saved.host.length <= 15 ? saved.host : "";
    const extra = typeof saved?.extra_app === "string" && saved.extra_app.length <= 80 ? saved.extra_app : "";
    const system = Object.hasOwn(standardSystems,saved?.system) ? saved.system : this._standardSystem(target);
    this._openForm("");
    this.diagnostic = {roomId:room?.id || "",targetId:target?.id || "",app:"netflix",observation:"not_tested",pending:false,report:null,standardReport:null,reportMode:"standard",standardHost:host,standardSystem:system,standardExtra:extra,advancedLoaded:false,error:"",message:"",assistant:null,bindingDraft:{host:"",firmware:"",app_version:""},bindingDirty:false,caseId:"",prepared:false,outcome:"not_tested",confirmTitle:false};
    this._renderDiagnosticForm();
    this.shadowRoot.querySelector("#standard-host")?.focus();
  }

  _standardSystem(target) {
    const platform = this.entity(target?.entity_id)?.platform || target?.profile;
    return platform === "androidtv_remote" ? "androidtv" : Object.hasOwn(standardSystems,platform) ? platform : target ? "other" : "samsungtv";
  }

  _activeDiagnosticReport(state = this.diagnostic) {
    return state?.reportMode === "standard" ? state.standardReport : state?.report;
  }

  _validDiagnosticReport(report) {
    return Boolean(report && typeof report === "object" && !Array.isArray(report) && (([1,2].includes(report.schema_version) && ["passive","guided"].includes(report.mode)) || (report.schema_version === 3 && report.mode === "standard")) && new Blob([JSON.stringify(report)]).size <= 500 * 1024);
  }

  _standardConnectionSummary(report) {
    const connection = report?.connection || {};
    const reachable = Array.isArray(report?.checks) && report.checks.some(check=>check?.id === "samsung_device_api" && check.http_status === 200);
    const reasons = {
      connection_refused:"Der Fernsteuerungs-Anschluss nimmt die Verbindung nicht an.",
      connection_failed:"Die Fernsteuerungs-Verbindung konnte nicht aufgebaut werden.",
      connection_timeout:"Der Aufbau der Fernsteuerungs-Verbindung hat zu lange gedauert.",
      tls_failed:"Die verschlüsselte Verbindung konnte nicht aufgebaut werden.",
      handshake_failed:"Die Fernsteuerungs-Sitzung konnte nicht geöffnet werden.",
      websocket_handshake_failed:"Die Fernsteuerungs-Sitzung konnte nicht geöffnet werden.",
      channel_closed:"Der Fernseher beendet die Verbindung vor der Freigabe.",
      tv_denied:"Der Fernseher hat die Freigabe abgelehnt.",
      tv_authorization_timeout:"Innerhalb der Wartezeit kam keine Freigabe vom Fernseher an.",
      authorization_timeout:"Innerhalb der Wartezeit kam keine Freigabe vom Fernseher an.",
      tv_protocol_error:"Die Antwort des Fernsehers passt nicht zum erwarteten Verbindungsablauf.",
      token_missing:"Der Fernseher hat keinen Verbindungsschlüssel geliefert.",
      invalid_response:"Die Antwort des Fernsehers konnte nicht ausgewertet werden.",
      response_limit:"Die Antwort des Fernsehers war für diese Abfrage zu umfangreich.",
      pairing_not_completed:"Die Kopplung wurde nicht abgeschlossen.",
    };
    const phases = {connect:"Verbindungsaufbau",tls:"Verschlüsselung",websocket_handshake:"Start der Fernsteuerung",authorization:"Freigabe",response:"Antwort"};
    const details = value => [
      Number.isInteger(value.port) && value.port > 0 && value.port <= 65535 ? `Port ${value.port}` : "",
      phases[value.phase] || "",
      Number.isInteger(value.http_status) && value.http_status >= 100 && value.http_status <= 599 ? `HTTP ${value.http_status}` : "",
      Number.isInteger(value.close_code) && value.close_code >= 0 && value.close_code <= 4999 ? `Close-Code ${value.close_code}` : "",
    ].filter(Boolean).join(" · ");
    const technical = details(connection);
    const attempts = Array.isArray(connection.attempts) ? connection.attempts.slice(0,2).filter(item=>item && typeof item === "object").map(item=>[details(item),reasons[item.reason] || ""].filter(Boolean).join(" · ")).filter(Boolean) : [];
    const pairing = connection.pairing;
    const title = {unavailable:"Fernsteuerungs-Verbindung fehlt",paired:"Fernsteuerung verbunden",existing:"Vorhandene Fernsteuerungs-Freigabe verwendet",not_required:"Keine Samsung-Kopplung angefragt"}[pairing] || "Fernsteuerungs-Verbindung nicht geprüft";
    return `<div class="diagnostic-connection" role="group" aria-label="Verbindungsstatus">${reachable ? `<p class="small">Gerät erreichbar.</p>` : ""}<p><strong ${pairing === "unavailable" ? 'class="negative"' : ""}>${title}</strong></p>
      ${pairing === "unavailable" ? `<p class="small">Apps und Vorschau konnten über diese Verbindung nicht geprüft werden.</p><p class="small">${reasons[connection.reason] || "Die Freigabe ist nicht bestätigt. Weitere Hinweise stehen im Bericht."}</p>` : pairing === "existing" ? `<p class="small muted">Für diese Diagnose wurde eine bestehende Kopplung verwendet.</p>` : ""}
      ${technical || attempts.length ? `<details class="advanced-settings"><summary>Technische Details</summary>${technical ? `<p class="small muted">${esc(technical)}</p>` : ""}${attempts.map((attempt,index)=>`<p class="small muted">Versuch ${index+1}: ${esc(attempt)}</p>`).join("")}</details>` : ""}</div>`;
  }

  async _collectStandardDiagnostic() {
    const state = this.diagnostic;
    if (!state || state.pending || this.pending || !this.data.can_configure || !state.standardHost?.trim() || !Object.hasOwn(standardSystems,state.standardSystem)) return;
    state.pending = true;
    state.action = "standard_diagnose";
    state.reportMode = "standard";
    state.standardReport = null;
    state.error = "";
    state.message = "";
    state.assistant = null;
    state.advancedLoaded = false;
    this.shadowRoot.querySelector("#standard-advanced")?.removeAttribute("open");
    this._renderDiagnosticForm();
    try {
      const data = {host:state.standardHost.trim(),system:state.standardSystem,extra_app:(state.standardExtra || "").trim()};
      try { localStorage.setItem("cinema-control-standard-diagnostic",JSON.stringify(data)); } catch { /* This convenience does not affect the request. */ }
      const report = await this._command("standard_diagnose", {data});
      if (this.diagnostic !== state) return;
      if (!this._validDiagnosticReport(report) || report.schema_version !== 3 || report.mode !== "standard" || !Array.isArray(report.app_results) || report.app_results.length > 7) throw new Error("Der Assistent hat keinen gültigen Standardbericht geliefert. Bitte erneut versuchen.");
      state.standardReport = report;
      state.message = "Der gemeinsame Bericht für die Standard-Apps ist bereit.";
    } catch (error) {
      if (this.diagnostic !== state) return;
      state.error = typeof error?.message === "string" ? error.message.slice(0,300) : "Die Standarddiagnose konnte nicht erstellt werden. Bitte erneut versuchen.";
    } finally {
      state.pending = false;
      if (this.diagnostic === state) {
        this._renderDiagnosticForm();
        this.shadowRoot.querySelector(state.standardReport ? '[data-op="diagnostic-download"]' : '[data-op="standard-collect"]')?.focus();
      }
    }
  }

  _advancedDiagnosticFields(state, room, target, busy) {
    const choices = this._diagnosticApps(room,target);
    if (!choices.some(choice=>choice.value === state.app)) state.app = choices[0]?.value || "";
    return `<details id="standard-advanced" class="advanced-settings assistant-details" ${state.advancedOpen ? "open" : ""}><summary>Starttests und weitere Details</summary>
      <p class="small muted">Zusätzliche Prüfungen für ein bereits in Home Assistant eingerichtetes Gerät. Dieses Ziel ist unabhängig von der oben eingegebenen TV-Adresse.</p><p class="small"><strong>${esc(room.name)} · ${esc(target.name)}</strong></p>
      <details id="standard-legacy-device" class="advanced-settings" ${state.legacyDeviceOpen ? "open" : ""}><summary>Anderes Gerät auswählen</summary><div class="form-grid"><label>Raum<select id="diagnostic-room" ${busy ? "disabled" : ""}>${this.data.rooms.filter(item=>item.targets.length).map(item=>`<option value="${esc(item.id)}" ${item.id===room.id?"selected":""}>${esc(item.name)}</option>`).join("")}</select></label><label>Wiedergabeziel<select id="diagnostic-target" ${busy ? "disabled" : ""}>${room.targets.map(item=>`<option value="${esc(item.id)}" ${item.id===target.id?"selected":""}>${esc(item.name)}</option>`).join("")}</select></label></div></details>
      <div class="form-grid"><label>App<select id="diagnostic-app" ${busy ? "disabled" : ""}>${choices.map(choice=>`<option value="${esc(choice.value)}" ${choice.value===state.app?"selected":""}>${esc(choice.name)}</option>`).join("")}</select></label>
      <label>Bisheriges Ergebnis (optional)<select id="diagnostic-observation" ${busy ? "disabled" : ""}>${Object.entries(diagnosticObservations).map(([value,name])=>`<option value="${value}" ${value===state.observation?"selected":""}>${name}</option>`).join("")}</select></label></div>
      <div class="assistant-actions"><button type="button" data-op="diagnostic-collect" ${busy || !state.app ? "disabled" : ""}>Einzelne App untersuchen</button></div>
      ${this._assistantConnection(state,target,busy)}${this._assistantTests(state,busy)}</details>`;
  }

  _assistantCanSave(status) {
    const session = status?.session;
    return Boolean(session?.can_save_profile && Array.isArray(session.cases) && session.cases.length === 6 && session.cases.every(item=>item.state === "recorded" && item.outcome === "playing"));
  }

  _assistantConnection(state, target, busy) {
    const draft = state.bindingDraft || {};
    const paired = state.assistant?.binding?.paired === true;
    const samsung = this.entity(target.entity_id)?.platform === "samsungtv" || target.profile === "samsungtv";
    return `<details id="assistant-connection" class="advanced-settings assistant-details" ${state.connectionOpen ? "open" : ""}><summary>Verbindung zum Fernseher${paired ? " · verbunden" : ""}</summary>
      <p class="small muted">Für einen weitergeleiteten Fernseher kannst du seine lokale Adresse hinterlegen. Firmware und App-Version sind optionale Angaben von dir.</p>
      <div class="form-grid"><label>Lokale TV-Adresse (optional)<input id="assistant-host" value="${esc(draft.host)}" maxlength="15" inputmode="decimal" placeholder="192.168.1.50" ${busy ? "disabled" : ""}></label>
      <label>Firmware (optional)<input id="assistant-firmware" value="${esc(draft.firmware)}" maxlength="120" placeholder="Version aus den TV-Einstellungen" ${busy ? "disabled" : ""}></label>
      <label>App-Version (optional)<input id="assistant-app-version" value="${esc(draft.app_version)}" maxlength="80" ${busy ? "disabled" : ""}></label></div>
      <div class="assistant-actions"><button type="button" data-op="assistant-bind" ${busy || !draft.host?.trim() ? "disabled" : ""}>Verbindung speichern</button>${samsung ? `<button type="button" data-op="assistant-pair" ${busy ? "disabled" : ""}>${paired ? "Samsung erneut verbinden" : "Samsung verbinden"}</button>` : ""}</div>
      ${samsung ? `<p class="small muted">„Samsung verbinden“ kann eine Zulassen-Abfrage am Fernseher öffnen. Dabei wird noch kein Titel gestartet.</p>` : ""}</details>`;
  }

  _assistantTests(state, busy) {
    const status = state.assistant;
    const favorites = this.data.favorites.filter(item=>item.provider === state.app);
    const adapters = Array.isArray(status?.adapters) ? status.adapters : [];
    if (!favorites.some(item=>item.id === state.favoriteA)) state.favoriteA = favorites[0]?.id || "";
    if (!favorites.some(item=>item.id === state.favoriteB)) state.favoriteB = favorites.find(item=>item.id !== state.favoriteA)?.id || "";
    if (!adapters.some(item=>item.id === state.adapterId)) state.adapterId = adapters.find(item=>item.can_test)?.id || adapters[0]?.id || "";
    const adapter = adapters.find(item=>item.id === state.adapterId);
    const session = status?.session;
    const cases = Array.isArray(session?.cases) ? session.cases : [];
    if (!cases.some(item=>item.id === state.caseId)) state.caseId = cases.find(item=>item.state === "awaiting_observation")?.id || cases.find(item=>item.state === "planned")?.id || cases[0]?.id || "";
    const testCase = cases.find(item=>item.id === state.caseId);
    const unresolved = cases.some(item=>["running","awaiting_observation"].includes(item.state));
    const caseTitle = item => `${this.data.favorites.find(favorite=>favorite.id === item.favorite_id)?.name || "Favorit"} · ${assistantScenarios[item.scenario]?.label || "Ausgangssituation"}`;
    const canPlan = !busy && !session && favorites.length >= 2 && state.favoriteA && state.favoriteB && state.favoriteA !== state.favoriteB && adapter?.can_test;
    const canRun = !busy && testCase?.state === "planned" && !unresolved && state.prepared;
    const canRecord = !busy && testCase?.state === "awaiting_observation" && state.outcome !== "not_tested" && (state.outcome !== "playing" || state.confirmTitle);
    const caseState = {planned:"Noch nicht gestartet",running:"Aufruf läuft",awaiting_observation:"Ergebnis am Fernseher prüfen",recorded:assistantOutcomes[testCase?.outcome] || "Ergebnis gespeichert",blocked:"Kein Start möglich. Hinweise stehen im Diagnosebericht."}[testCase?.state] || "";
    const profiles = Array.isArray(status?.profiles) ? status.profiles : [];
    const recheck = profiles.some(profile=>profile.requires_recheck || profile.matches_current_report === false);
    const unknownVersions = !state.report?.device?.firmware || !state.report?.app?.version || state.report?.app?.version_source === "user";
    return `<details id="assistant-tests" class="advanced-settings assistant-details" ${state.testsOpen || session ? "open" : ""}><summary>Geführter Testlauf${session ? ` · ${cases.filter(item=>item.state === "recorded").length} von ${cases.length}` : ""}</summary>
      <p class="small muted">Zwei unterschiedliche Titel, jeweils mit geöffneter App, geschlossener App und Profilauswahl. Du bereitest die Ausgangssituation am TV vor und startest jeden Test einzeln.</p>
      ${!status ? `<p class="small muted">Die verfügbaren Startwege werden geladen.</p>` : !session ? `${favorites.length < 2 ? `<p>Lege zuerst zwei unterschiedliche Favoriten für ${esc(labels[state.app] || "diese App")} an.</p>` : `<div class="form-grid"><label>Erster Favorit<select id="assistant-favorite-a" ${busy ? "disabled" : ""}>${favorites.map(item=>`<option value="${esc(item.id)}" ${item.id === state.favoriteA ? "selected" : ""}>${esc(item.name)}</option>`).join("")}</select></label><label>Zweiter Favorit<select id="assistant-favorite-b" ${busy ? "disabled" : ""}>${favorites.map(item=>`<option value="${esc(item.id)}" ${item.id === state.favoriteB ? "selected" : ""}>${esc(item.name)}</option>`).join("")}</select></label></div>`}
      <label>Startweg<select id="assistant-adapter" ${busy || !adapters.length ? "disabled" : ""}>${adapters.length ? adapters.map(item=>`<option value="${esc(item.id)}" ${item.id === state.adapterId ? "selected" : ""}>${esc(item.label || "Startweg")}${item.can_test ? "" : " · nicht verfügbar"}</option>`).join("") : `<option>Kein Startweg verfügbar</option>`}</select></label>
      ${adapter?.reason ? `<p class="small muted">${esc(adapter.reason)}</p>` : ""}<button type="button" data-op="assistant-plan" ${canPlan ? "" : "disabled"}>Testlauf vorbereiten</button>` : `<label>Einzelner Test<select id="assistant-case" ${busy ? "disabled" : ""}>${cases.map(item=>`<option value="${esc(item.id)}" ${item.id === state.caseId ? "selected" : ""}>${esc(caseTitle(item))}</option>`).join("")}</select></label>
      <p class="assistant-case-state" role="status">${esc(caseState)}</p>
      ${testCase?.state === "planned" ? `<p>${esc(assistantScenarios[testCase.scenario]?.instruction || "Bereite den Fernseher für diesen Test vor.")}</p><label class="assistant-check"><input id="assistant-prepared" type="checkbox" ${state.prepared ? "checked" : ""} ${busy || unresolved ? "disabled" : ""}><span>Am Fernseher so vorbereitet</span></label><button type="button" data-op="assistant-run" ${canRun ? "" : "disabled"}>Diesen Test starten</button>${unresolved ? `<p class="small muted">Notiere zuerst das Ergebnis des bereits gestarteten Tests.</p>` : ""}` : ""}
      ${testCase?.state === "awaiting_observation" ? `<label>Was ist am Fernseher zu sehen?<select id="assistant-outcome" ${busy ? "disabled" : ""}>${Object.entries(assistantOutcomes).map(([value,name])=>`<option value="${value}" ${value === state.outcome ? "selected" : ""}>${name}</option>`).join("")}</select></label>${state.outcome === "playing" ? `<label class="assistant-check"><input id="assistant-confirm-title" type="checkbox" ${state.confirmTitle ? "checked" : ""} ${busy ? "disabled" : ""}><span>Ich habe geprüft, dass der richtige Titel am Fernseher läuft.</span></label>` : ""}<button type="button" data-op="assistant-record" ${canRecord ? "" : "disabled"}>Ergebnis festhalten</button>` : ""}
      <div class="assistant-actions"><button type="button" data-op="assistant-save-profile" ${!busy && this._assistantCanSave(status) ? "" : "disabled"}>Bestätigtes Startprofil speichern</button><button type="button" data-op="assistant-cancel" ${busy ? "disabled" : ""}>Testlauf beenden</button></div><p class="small muted">Ein Startprofil kann erst nach sechs bestätigten Titelstarts gespeichert werden.</p>`}
      ${profiles.length ? `<p class="small muted">Gespeicherte lokale Startprofile für diese Kombination: ${profiles.length}. Sie gelten für dieses Gerät und diese App.</p>${recheck ? `<p class="small negative">Versions- oder Geräteangaben passen nicht mehr zum gespeicherten Profil. Prüfe den Startweg erneut.</p>` : ""}` : ""}
      ${unknownVersions ? `<p class="small muted">Ohne bestätigte Firmware- und App-Version bleibt ein Startprofil auf dieses lokal getestete Gerät begrenzt.</p>` : ""}</details>`;
  }

  _renderDiagnosticForm() {
    const state = this.diagnostic;
    if (!state) return;
    const connection = this.shadowRoot.querySelector("#assistant-connection"), tests = this.shadowRoot.querySelector("#assistant-tests");
    if (connection) state.connectionOpen = connection.open;
    if (tests) state.testsOpen = tests.open;
    const existingAdvanced = this.shadowRoot.querySelector("#standard-advanced");
    if (existingAdvanced) state.advancedOpen = existingAdvanced.open;
    const existingDeviceChooser = this.shadowRoot.querySelector("#standard-legacy-device");
    if (existingDeviceChooser) state.legacyDeviceOpen = existingDeviceChooser.open;
    const room = this.data.rooms.find(item=>item.id === state.roomId);
    const target = room?.targets.find(item=>item.id === state.targetId);
    const report = this._activeDiagnosticReport(state);
    const busy = state.pending || this.pending;
    const publicValue = value => typeof value === "string" && value ? value : "Unbekannt";
    const systemName = {...standardSystems,androidtv_remote:"Android TV",roku:"Roku"}[report?.platform] || "Unbekannt";
    const changedFields = report?.diff?.changed_fields || report?.changes?.changed_fields;
    const fieldLabels = {manufacturer:"Hersteller",model:"Modell",firmware:"Firmware",app_version:"App-Version",platform:"TV-System",preview:"Vorschau",checks:"Verbindung",candidates:"Startwege"};
    const changes = Array.isArray(changedFields) ? [...new Set(changedFields.map(value=>typeof value === "string" ? fieldLabels[value.split(".").pop()] || "Weitere Angaben" : "Weitere Angaben"))] : [];
    const limitations = Array.isArray(report?.limitations) ? report.limitations : [];
    const limitationLabels = {passive_no_playback_test:"Die Diagnose allein bestätigt keine Wiedergabe.",no_automatic_profile_changes:"Ein Startprofil wird nur nach deiner Bestätigung gespeichert.",main_instance_does_not_proxy_tv_network:"Die Hauptinstanz reicht keinen direkten TV-Zugriff weiter.",firmware_not_reported:"Das Gerät hat keine Firmware-Version gemeldet.",application_name_omitted:"Der App-Name wurde zum Datenschutz ausgelassen.",firmware_user_reported:"Die Firmware wurde von dir angegeben und nicht am Gerät bestätigt.",existing_pairing_required:"Für die Vorschau-Abfrage fehlt eine bestehende TV-Verbindung.",reported_versions_are_user_hints:"Selbst eingetragene Versionen sind Hinweise und keine Gerätebestätigung.",reported_firmware_differs:"Deine Firmware-Angabe weicht von der gemeldeten Version ab. Prüfe sie am Fernseher.",device_identity_mismatch:"Die Adresse gehört offenbar zu einem anderen Gerät. Prüfe die TV-Adresse, bevor du fortfährst."};
    const limitationMessages = [...new Set(limitations.map(value=>limitationLabels[typeof value === "string" ? value : value?.id] || "Weitere technische Hinweise stehen im Bericht."))];
    const pendingMessage = {standard_diagnose:"Verbindung und Standard-Apps werden geprüft. Bestätige „Zulassen“ am Samsung, falls die Abfrage erscheint. Das kann einen Moment dauern …",status:"Verbindung und vorhandene Testläufe werden gelesen …",bind:"Verbindung wird gespeichert …",pair:"Bitte die Verbindung am Samsung zulassen …",plan:"Testlauf wird vorbereitet …",run:"Der gewählte Titelaufruf wird gesendet …",record:"Dein Ergebnis wird festgehalten …",save_profile:"Das bestätigte Startprofil wird gespeichert …",cancel:"Testlauf wird beendet …"}[state.action] || "Geräteinformationen werden gelesen …";
    this.shadowRoot.querySelector("#form").innerHTML = `${this._formHeader("Direktstart-Assistent")}
      <p class="small muted">Schalte den Fernseher ein. Bei Samsung kann eine Zulassen-Abfrage erscheinen. Die Standarddiagnose startet keine Filme oder Apps.</p>
      <div class="form-grid diagnostic-fields"><label>TV-IP-Adresse<input id="standard-host" value="${esc(state.standardHost)}" maxlength="15" inputmode="decimal" placeholder="192.168.1.50" required ${busy ? "disabled" : ""}></label><label>TV-System<select id="standard-system" ${busy ? "disabled" : ""}>${Object.entries(standardSystems).map(([value,name])=>`<option value="${value}" ${state.standardSystem===value?"selected":""}>${name}</option>`).join("")}</select></label></div>
      <p class="small muted standard-app-caption">Diese Apps werden gemeinsam geprüft</p><ul class="standard-apps" aria-label="Standard-Apps">${standardApps.map(name=>`<li>${name}</li>`).join("")}</ul>
      <label>Weitere App (optional)<input id="standard-extra-app" value="${esc(state.standardExtra)}" maxlength="80" placeholder="Zum Beispiel Disney+" ${busy ? "disabled" : ""}></label>
      <div class="assistant-actions"><button type="button" class="primary" data-op="standard-collect" ${busy || !state.standardHost?.trim() ? "disabled" : ""}>${state.pending && state.action === "standard_diagnose" ? "Wird geprüft …" : "Verbinden und Standarddiagnose starten"}</button></div>
      <div id="diagnostic-status" class="diagnostic-status" role="status" aria-live="polite" aria-busy="${state.pending}">${state.pending ? esc(pendingMessage) : state.error ? `<span class="negative">${esc(state.error)}</span>` : state.message ? esc(state.message) : report ? "Der Bericht ist bereit." : "Die Diagnose installiert nichts auf dem Fernseher."}</div>
      ${report ? `<section class="diagnostic-result" aria-label="Diagnoseergebnis"><dl><div><dt>Hersteller</dt><dd>${esc(publicValue(report.device?.manufacturer))}</dd></div><div><dt>Modell</dt><dd>${esc(publicValue(report.device?.model))}</dd></div><div><dt>Firmware</dt><dd>${esc(publicValue(report.device?.firmware))}</dd></div><div><dt>${report.platform_source === "user_declared" ? "TV-System nach deiner Auswahl" : "System"}</dt><dd>${esc(systemName)}</dd></div></dl>${report.mode === "standard" ? this._standardConnectionSummary(report) : ""}${report.mode === "standard" && Array.isArray(report.app_results) ? `<ul class="standard-results" aria-label="Ergebnisse der App-Diagnose">${report.app_results.map(app=>`<li><strong>${esc(app.name || app.id || "App")}</strong><span>${{detected:"Erkannt",not_listed:"Nicht in der App-Liste gemeldet",not_checked:"Nicht geprüft"}[app.presence] || "Nicht geprüft"}</span></li>`).join("")}</ul>` : ""}<p class="small muted">Eine App-Liste bestätigt noch keinen funktionierenden Titelstart. Fehlende Angaben bleiben im Bericht als unbekannt markiert.</p></section>` : ""}
      ${report?.reported?.firmware || report?.reported?.app_version ? `<p class="small muted">Deine Versionsangaben: ${report.reported.firmware ? `Firmware ${esc(report.reported.firmware)}` : ""}${report.reported.firmware && report.reported.app_version ? " · " : ""}${report.reported.app_version ? `App ${esc(report.reported.app_version)}` : ""}. Diese Angaben wurden nicht am Gerät bestätigt.</p>` : ""}
      ${changes.length ? `<p class="small">Seit dem letzten Bericht geändert: ${changes.map(esc).join(", ")}.</p>` : ""}
      ${limitationMessages.length ? `<ul class="small muted diagnostic-limitations">${limitationMessages.map(message=>`<li>${esc(message)}</li>`).join("")}</ul>` : ""}
      ${room && target ? this._advancedDiagnosticFields(state,room,target,busy) : ""}
      <p class="small muted">Der Bericht wird nur auf deinen Wunsch heruntergeladen. Zugangsdaten und private Geräteadressen werden nicht exportiert.</p>
      <footer class="form-actions"><button type="button" data-op="dialog-close">Schließen</button><span></span>${report ? `<button type="button" class="primary" data-op="diagnostic-download" ${busy ? "disabled" : ""}>Bericht herunterladen</button>` : ""}</footer>`;
    const advanced = this.shadowRoot.querySelector("#standard-advanced");
    advanced?.addEventListener("toggle",()=>{
      if (this.diagnostic !== state) return;
      state.advancedOpen = advanced.open;
      if (advanced.open && !state.advancedLoaded && !state.pending && !this.pending) {
        state.advancedLoaded = true;
        void this._assistantAction("status");
      }
    });
  }

  _diagnosticChanged(element) {
    const state = this.diagnostic;
    if (!state || state.pending) return;
    const focusId = element.id;
    if (element.id === "diagnostic-room") {
      state.roomId = element.value;
      state.targetId = this.data.rooms.find(room=>room.id === state.roomId)?.targets[0]?.id;
    } else if (element.id === "diagnostic-target") state.targetId = element.value;
    else if (element.id === "diagnostic-app") state.app = element.value;
    else if (element.id === "diagnostic-observation" && Object.hasOwn(diagnosticObservations, element.value)) state.observation = element.value;
    state.report = null;
    state.error = "";
    state.message = "";
    const changedDevice = ["diagnostic-room","diagnostic-target","diagnostic-app"].includes(element.id);
    if (changedDevice) {
      state.advancedLoaded = true;
      state.assistant = null;
      state.bindingDraft = {host:"",firmware:"",app_version:""};
      state.bindingDirty = false;
      state.caseId = "";
      state.prepared = false;
      state.outcome = "not_tested";
      state.confirmTitle = false;
    }
    this._renderDiagnosticForm();
    this.shadowRoot.getElementById(focusId)?.focus();
    if (changedDevice) void this._assistantAction("status");
  }

  _assistantChanged(element) {
    const state = this.diagnostic;
    if (!state || state.pending) return;
    const focusId = element.id;
    if (element.id === "assistant-favorite-a") state.favoriteA = element.value;
    else if (element.id === "assistant-favorite-b") state.favoriteB = element.value;
    else if (element.id === "assistant-adapter") state.adapterId = element.value;
    else if (element.id === "assistant-case") {
      state.caseId = element.value;
      state.prepared = false;
      state.outcome = "not_tested";
      state.confirmTitle = false;
    } else if (element.id === "assistant-prepared") state.prepared = element.checked;
    else if (element.id === "assistant-outcome" && Object.hasOwn(assistantOutcomes, element.value)) {
      state.outcome = element.value;
      state.confirmTitle = false;
    } else if (element.id === "assistant-confirm-title") state.confirmTitle = element.checked;
    else return;
    state.error = "";
    this._renderDiagnosticForm();
    this.shadowRoot.getElementById(focusId)?.focus();
  }

  async _assistantAction(action) {
    const state = this.diagnostic;
    if (!state || state.pending || this.pending || !this.data.can_configure) return;
    const session = state.assistant?.session;
    const testCase = session?.cases?.find(item=>item.id === state.caseId);
    let data = {};
    if (["pair","plan","run"].includes(action) && state.bindingDirty) {
      state.error = "Speichere zuerst die geänderte Verbindung.";
      this._renderDiagnosticForm();
      return;
    }
    if (action === "bind") {
      if (!state.bindingDraft?.host?.trim()) return;
      data = {...state.bindingDraft};
    } else if (action === "plan") {
      if (!state.favoriteA || !state.favoriteB || state.favoriteA === state.favoriteB) return;
      data = {favorite_a:state.favoriteA,favorite_b:state.favoriteB,adapter_id:state.adapterId};
    } else if (action === "run") {
      if (!session || testCase?.state !== "planned" || !state.prepared || session.cases.some(item=>["running","awaiting_observation"].includes(item.state))) return;
      data = {session_id:session.id,case_id:testCase.id,prepared:true};
    } else if (action === "record") {
      if (!session || testCase?.state !== "awaiting_observation" || !Object.hasOwn(assistantOutcomes,state.outcome) || state.outcome === "not_tested" || (state.outcome === "playing" && !state.confirmTitle)) return;
      data = {session_id:session.id,case_id:testCase.id,outcome:state.outcome};
    } else if (["save_profile","cancel"].includes(action)) {
      if (!session || (action === "save_profile" && !this._assistantCanSave(state.assistant))) return;
      data = {session_id:session.id};
    } else if (!["status","pair"].includes(action)) return;
    state.pending = true;
    state.action = action;
    state.error = "";
    state.message = "";
    this._renderDiagnosticForm();
    try {
      const result = await this._command("assistant", {room_id:state.roomId,target_id:state.targetId,app:state.app,action,data});
      if (this.diagnostic !== state) return;
      if (!result || typeof result !== "object" || Array.isArray(result) || !result.binding || typeof result.binding !== "object" || new Blob([JSON.stringify(result)]).size > 500 * 1024) throw new Error("Der Assistent hat keine gültige Antwort geliefert. Bitte erneut versuchen.");
      state.assistant = result;
      state.report = this._validDiagnosticReport(result.report) ? result.report : null;
      if (["plan","run","record","save_profile"].includes(action)) state.reportMode = "advanced";
      if ((["status","bind","pair"].includes(action) && !state.bindingDirty) || action === "bind") {
        state.bindingDraft = Object.fromEntries(["host","firmware","app_version"].map(key=>[key,typeof result.binding[key] === "string" ? result.binding[key] : ""]));
        state.bindingDirty = false;
      }
      if (["run","record","plan","cancel"].includes(action)) {
        state.prepared = false;
        state.outcome = "not_tested";
        state.confirmTitle = false;
      }
      state.message = {bind:"Verbindung gespeichert.",pair:"Samsung ist verbunden.",plan:"Testlauf vorbereitet. Es wurde noch nichts gestartet.",run:"Der Aufruf wurde gesendet. Prüfe jetzt Titel und Wiedergabe am Fernseher.",record:"Dein Ergebnis wurde festgehalten.",save_profile:"Das bestätigte Startprofil wurde lokal gespeichert.",cancel:"Der Testlauf wurde beendet."}[action] || "";
      if (action === "run" && result.session?.cases?.find(item=>item.id === data.case_id)?.state === "blocked") state.message = "Kein Start möglich. Lade den Diagnosebericht herunter, um die Hinweise weiterzugeben.";
    } catch (error) {
      if (this.diagnostic !== state) return;
      state.error = typeof error?.message === "string" ? error.message.slice(0, 300) : "Die Aktion konnte nicht abgeschlossen werden. Bitte erneut versuchen.";
    } finally {
      state.pending = false;
      if (this.diagnostic === state) {
        this._renderDiagnosticForm();
        if (action === "run") this.shadowRoot.querySelector("#assistant-outcome")?.focus();
        else if (action === "record") this.shadowRoot.querySelector("#assistant-case")?.focus();
      }
    }
  }

  async _collectDiagnostic() {
    const state = this.diagnostic;
    if (!state || state.pending || this.pending || !this.data.can_configure) return;
    state.pending = true;
    state.action = "diagnose";
    state.reportMode = "advanced";
    state.report = null;
    state.error = "";
    state.message = "";
    this._renderDiagnosticForm();
    try {
      const report = await this._command("diagnose", {room_id:state.roomId,target_id:state.targetId,app:state.app,observation:state.observation});
      if (this.diagnostic !== state) return;
      if (!this._validDiagnosticReport(report)) throw new Error("Die Diagnose hat keinen gültigen Bericht geliefert. Bitte erneut versuchen.");
      state.report = report;
    } catch (error) {
      if (this.diagnostic !== state) return;
      state.error = typeof error?.message === "string" ? error.message.slice(0, 300) : "Der Bericht konnte nicht erstellt werden. Bitte erneut versuchen.";
    } finally {
      state.pending = false;
      if (this.diagnostic === state) {
        const refreshAssistant = Boolean(state.report && !state.error);
        if (refreshAssistant) state.assistant = null;
        this._renderDiagnosticForm();
        this.shadowRoot.querySelector(state.report ? '[data-op="diagnostic-download"]' : '[data-op="diagnostic-collect"]')?.focus();
        if (refreshAssistant) void this._assistantAction("status");
      }
    }
  }

  _downloadDiagnostic() {
    const report = this._activeDiagnosticReport();
    if (!report || this.pending || this.diagnostic.pending) return;
    const file = new Blob([JSON.stringify(report, null, 2) + "\n"], {type:"application/json;charset=utf-8"});
    if (file.size > 500 * 1024) return;
    const url = URL.createObjectURL(file);
    const link = document.createElement("a");
    link.href = url;
    link.download = "kinosteuerung-direktstart-diagnose.json";
    link.hidden = true;
    this.shadowRoot.append(link);
    link.click();
    link.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
  }

  _favoriteForm(favorite = null) {
    this.editRevision = this.data.revision;
    this.editFavoriteId = favorite?.id;
    this._openForm(`<form id="favorite-form">${this._formHeader(favorite ? "Favorit bearbeiten" : "Favorit hinzufügen")}
      <div class="favorite-form-grid"><div class="favorite-fields">
        <label>Filmname<input name="name" value="${esc(favorite?.name)}" maxlength="160" placeholder="Name des Films, der Serie oder des Videos"></label>
        <label>Link zum Film, zur Folge oder zum Video<input name="url" type="url" value="${esc(favorite?.url)}" maxlength="2048" placeholder="https://…" required><span class="small muted">Der Streamingdienst wird aus dem Link erkannt. Für Plex ist auch plex://movie/466 möglich.</span></label>
      </div><div class="favorite-image-field"><label id="favorite-image-label">Bild (optional)</label>
        <input name="image" type="hidden" value="${esc(favorite?.image)}">
        <input id="favorite-image-file" type="file" accept="image/jpeg,image/png,image/webp" hidden>
        <div id="favorite-image-drop" class="image-dropzone" role="group" aria-labelledby="favorite-image-label" aria-busy="false">
          <img id="favorite-image-preview" class="upload-preview" ${favorite?.image ? `src="${esc(favorite.image)}"` : "hidden"} alt="Vorschau des Favoritenbildes">
          <div class="image-drop-content">${icon("movie")}<strong>Bild hier ablegen</strong><p class="small muted">JPG, PNG oder WebP · bis 5 MB</p><button type="button" data-op="image-choose">Bild auswählen</button></div>
        </div><div class="image-upload-actions"><button type="button" data-op="image-remove" ${favorite?.image ? "" : "hidden"}>Bild entfernen</button></div>
        <p id="favorite-upload-status" class="small upload-status" role="status" aria-live="polite"></p>
      </div></div>
      <details class="advanced-settings" ${favorite?.playback === "restart" || favorite?.prime_auto_profile ? "open" : ""}><summary>Weitere Optionen</summary>
        <div id="prime-profile-option" hidden><label><span><input type="checkbox" name="prime_auto_profile" ${favorite?.prime_auto_profile ? "checked" : ""}> Markiertes Prime-Video-Profil automatisch bestätigen</span><span class="small muted">Aktivieren, wenn dein gewünschtes Profil beim Start markiert ist.</span></label><label><span><input type="checkbox" name="prime_auto_continue" ${favorite?.prime_auto_continue ? "checked" : ""}> Fortsetzen automatisch bestätigen</span><span class="small muted">Bestätigt nach der Wartezeit des Raums einmal die markierte Wiedergabe.</span></label></div>
        <label>Beim Titel öffnen<select name="playback"><option value="resume">Fortsetzen</option><option value="restart" ${favorite?.playback === "restart" ? "selected" : ""}>Von Anfang an</option></select><span class="small muted">Von Anfang an benötigt eine Unterstützung für Positionswechsel.</span></label>
        <label>Vorhandener lokaler Bildpfad<input id="favorite-image-path" value="${esc(favorite?.image)}" maxlength="512" placeholder="/local/poster.jpg"></label>
      </details>
      <footer class="form-actions">${favorite ? `<button type="button" class="danger" data-op="favorite-delete" data-id="${esc(favorite.id)}">Favorit löschen</button>` : ""}<span></span><button type="button" data-op="dialog-close">Abbrechen</button><button type="submit" class="primary">Favorit speichern</button></footer>
    </form>`);
    const form = this.shadowRoot.querySelector("#favorite-form");
    const updatePrimeOption = () => {
      const isPrime = /^https:\/\/(?:www\.)?primevideo\.com\//i.test(form.elements.url.value.trim());
      form.querySelector("#prime-profile-option").hidden = !isPrime;
      form.elements.prime_auto_profile.disabled = !isPrime;
      form.elements.prime_auto_continue.disabled = !isPrime || !form.elements.prime_auto_profile.checked;
    };
    form.elements.url.addEventListener("input", updatePrimeOption);
    form.elements.prime_auto_profile.addEventListener("change", updatePrimeOption);
    form.querySelector("#favorite-image-file").addEventListener("change", event => {
      const file = event.target.files?.[0];
      if (file) this._uploadFavoriteImage(file);
      event.target.value = "";
    });
    const zone = form.querySelector("#favorite-image-drop");
    for (const type of ["dragenter", "dragover"]) zone.addEventListener(type, event => {
      event.preventDefault();
      if (!this.imageUpload) zone.classList.add("is-dragging");
    });
    zone.addEventListener("dragleave", event => {
      if (!zone.contains(event.relatedTarget)) zone.classList.remove("is-dragging");
    });
    zone.addEventListener("drop", event => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
      const files = event.dataTransfer?.files;
      if (files?.length === 1) this._uploadFavoriteImage(files[0]);
      else this._uploadStatus("Bitte genau ein Bild auswählen.", true);
    });
    form.querySelector("#favorite-image-path").addEventListener("change", event => {
      if (!this.imageUpload) this._setFavoriteImage(event.target.value.trim());
    });
    updatePrimeOption();
    form.elements.name.focus();
  }

  _uploadStatus(message, failed = false) {
    const status = this.shadowRoot.querySelector("#favorite-upload-status");
    if (status) {
      status.textContent = message;
      status.classList.toggle("negative", failed);
    }
  }

  _setFavoriteImage(path) {
    const form = this.shadowRoot.querySelector("#favorite-form");
    if (!form) return;
    form.elements.image.value = path;
    form.querySelector("#favorite-image-path").value = path;
    const preview = form.querySelector("#favorite-image-preview");
    if (path && path.startsWith("/local/")) preview.src = path;
    else preview.removeAttribute("src");
    preview.hidden = !path || !path.startsWith("/local/");
    form.querySelector("[data-op=image-remove]").hidden = !path;
  }

  _cancelImageUpload() {
    this.imageUpload?.controller.abort();
    this.imageUpload = null;
    if (this.imagePreviewUrl) URL.revokeObjectURL(this.imagePreviewUrl);
    this.imagePreviewUrl = null;
    const form = this.shadowRoot.querySelector("#favorite-form");
    if (form) {
      form.querySelector("#favorite-image-drop").setAttribute("aria-busy", "false");
      for (const button of form.querySelectorAll("[type=submit],[data-op=image-choose]")) button.disabled = false;
    }
  }

  async _uploadFavoriteImage(file) {
    const form = this.shadowRoot.querySelector("#favorite-form");
    if (!form || this.imageUpload) return;
    const supported = ["image/jpeg", "image/png", "image/webp"].includes(file.type) || (!file.type && /\.(jpe?g|png|webp)$/i.test(file.name));
    if (!supported) { this._uploadStatus("Bitte ein JPG-, PNG- oder WebP-Bild auswählen.", true); return; }
    if (!file.size || file.size > 5 * 1024 * 1024) { this._uploadStatus("Das Bild darf höchstens 5 MB groß sein.", true); return; }
    const upload = this.imageUpload = {controller: new AbortController(), form};
    const previous = form.elements.image.value;
    form.querySelector("#favorite-image-drop").setAttribute("aria-busy", "true");
    for (const button of form.querySelectorAll("[type=submit],[data-op=image-choose]")) button.disabled = true;
    if (this.imagePreviewUrl) URL.revokeObjectURL(this.imagePreviewUrl);
    this.imagePreviewUrl = URL.createObjectURL(file);
    const preview = form.querySelector("#favorite-image-preview");
    preview.src = this.imagePreviewUrl;
    preview.hidden = false;
    this._uploadStatus("Bild wird hochgeladen …");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await this._hass.fetchWithAuth("/api/cinema_control/image", {method:"POST", body, signal:upload.controller.signal});
      const result = await response.json().catch(() => ({}));
      if (this.imageUpload !== upload || !form.isConnected) return;
      if (!response.ok) throw new Error(response.status === 413 ? "Das Bild darf höchstens 5 MB groß sein." : typeof result.message === "string" ? result.message.slice(0,240) : "Das Bild konnte nicht hochgeladen werden.");
      if (typeof result.url !== "string" || !/^\/local\/cinema_control\/posters\/[a-f0-9]{64}\.jpg$/.test(result.url)) throw new Error("Der Server hat keinen gültigen Bildpfad zurückgegeben.");
      this._setFavoriteImage(result.url);
      this._uploadStatus("Bild hochgeladen. Speichere den Favoriten, um es zu übernehmen.");
    } catch (error) {
      if (this.imageUpload !== upload || !form.isConnected) return;
      this._setFavoriteImage(previous);
      this._uploadStatus(error.message || "Das Bild konnte nicht hochgeladen werden.", true);
    } finally {
      if (this.imageUpload === upload) this._cancelImageUpload();
    }
  }

  _entityOptions(domain, selected, optional = false) {
    return `${optional ? '<option value="">Keine Fernbedienung</option>' : '<option value="">Bitte wählen</option>'}${this.data.entities.filter(e=>e.entity_id.startsWith(domain+".")).map(e=>`<option value="${esc(e.entity_id)}" ${e.entity_id===selected?"selected":""}>${esc(e.name)} · ${esc(states[e.state] || e.state)}</option>`).join("")}`;
  }

  _roomForm(room = null) {
    this.editRevision = this.data.revision;
    this.roomDraft = room ? structuredClone(room) : {id:id(),name:"",tv_entity_id:"",targets:[]};
    this._openForm(`<form id="room-form">
      ${this._formHeader(room ? "Raum bearbeiten" : "Raum einrichten")}
      <section class="form-section">
        <p class="section-kicker">01 · Raum und Fernseher</p>
        <div class="form-grid">
          <label>Raumname<input name="name" value="${esc(room?.name)}" required maxlength="160" list="area-names"><datalist id="area-names">${this.data.areas.map(a=>`<option value="${esc(a.name)}">`).join("")}</datalist></label>
          <label>Fernseher<select name="tv_entity_id" id="draft-tv" required>${this._entityOptions("media_player",room?.tv_entity_id)}</select></label>
        </div>
        <p class="small muted">Dieser Fernseher regelt die Lautstärke und den Eingang.</p>
      </section>
      <section class="form-section">
        <p class="section-kicker">02 · Wiedergabeziele</p>
        <div class="section-head"><h3>Fernseher und Zuspieler</h3><button type="button" data-op="target-add">${icon("plus")} Ziel hinzufügen</button></div>
        <p class="small muted">Für TV-Apps wähle den Fernseher als Wiedergabegerät. Einen externen Zuspieler ordnest du seinem HDMI-Eingang zu.</p>
        <div id="targets-form">${this.roomDraft.targets.map(t=>this._targetForm(t)).join("")}</div>
      </section>
      <details class="form-section advanced-settings">
        <summary>Prime Video · Wartezeiten</summary>
        <p class="small muted">Für Favoriten mit automatischer Profilwahl und Wiedergabe.</p>
        <div class="form-grid">
          <label>Bis zum Profil-OK (Sekunden)<input name="prime_profile_delay" type="number" min="1" max="90" step="1" required value="${esc(room?.prime_profile_delay ?? 12)}"></label>
          <label>Bis zum Fortsetzen-OK (Sekunden)<input name="prime_continue_delay" type="number" min="1" max="90" step="1" required value="${esc(room?.prime_continue_delay ?? 20)}"><span class="small muted">Wartezeit nach dem Profil-OK.</span></label>
        </div>
      </details>
      <footer class="form-actions">${room ? `<button type="button" class="danger" data-op="room-delete" data-id="${esc(room.id)}">Raum löschen</button>` : ""}<span></span><button type="button" data-op="dialog-close">Abbrechen</button><button type="submit" class="primary">Raum speichern</button></footer>
    </form>`);
    this.shadowRoot.querySelector("#room-form").addEventListener("change", event => {
      if (event.target.name !== "mode") return;
      const state = event.target.closest("[data-provider]")?.querySelector("[data-app-state]");
      if (state) state.textContent = event.target.selectedOptions[0]?.textContent || "Nicht eingerichtet";
    });
  }

  _targetForm(target) {
    const ent = this.entity(target.entity_id);
    const nativeSources = ["samsungtv","generic"].includes(target.profile);
    const expandedProvider = Object.keys(appLabels).find(provider => ["app", "link"].includes(target.apps?.[provider]?.mode));
    return `<fieldset class="target-config" data-target="${esc(target.id)}">
      <legend>${esc(target.name || "Neues Wiedergabeziel")}</legend>
      <div class="target-heading"><p class="small muted">TV-App oder externer Player</p><button type="button" class="danger" data-op="target-remove">Ziel entfernen</button></div>
      <div class="form-grid">
        <label>Name<input name="target_name" value="${esc(target.name)}" required maxlength="160"></label>
        <label>Wiedergabegerät<select name="entity_id" required>${this._entityOptions("media_player",target.entity_id)}</select></label>
      </div>
      <details class="advanced-settings">
        <summary>Erweiterte Geräteoptionen</summary>
        <div class="form-grid">
          <label>Geräteprofil<select name="profile">${[["generic","Allgemein · Quellenwahl"],["samsungtv","Samsung Smart TV"],["androidtv","Fire TV / Android · ADB"],["androidtv_remote","Android TV Remote · Google TV"]].map(([k,v])=>`<option value="${k}" ${target.profile===k?"selected":""}>${v}</option>`).join("")}</select><span class="small muted">Passend zur Geräteintegration in Home Assistant.</span></label>
          <label>Fernbedienung<select name="remote_entity_id">${this._entityOptions("remote",target.remote_entity_id,true)}</select></label>
          <label>HDMI-Eingang am Fernseher<input name="hdmi_source" value="${esc(target.hdmi_source)}" list="tv-sources-${target.id}" maxlength="160" placeholder="Bei TV-Apps leer lassen"><datalist id="tv-sources-${target.id}">${this._reportedSourceNames(this.roomDraft.tv_entity_id).map(s=>`<option value="${esc(s)}">`).join("")}</datalist></label>
        </div>
      </details>
      <div class="provider-list">
        <p class="section-kicker">Streaming-Apps</p>
        <p class="small muted">${nativeSources ? "App-Namen werden aus den Quellen dieses Geräts automatisch zugeordnet. Eine abweichende Quelle kannst du bei Bedarf auswählen." : "App-Zuordnung und Prüfnachweise pro Dienst."}</p>
        ${Object.entries(appLabels).map(([provider,name])=>{
        const app = target.apps?.[provider] || {mode:"off",app_id:"",tested:"untested"};
        const state = {off:"Nicht eingerichtet",app:"Nur App öffnen",link:"Inhaltslink senden"}[app.mode] || "Nicht eingerichtet";
        return `<details class="app-config provider-settings" data-provider="${provider}" name="providers-${esc(target.id)}" ${provider === expandedProvider ? "open" : ""}>
          <summary><span>${name}</span><span class="provider-state" data-app-state>${state}</span></summary>
          <div class="form-grid">
            <label>Startverfahren<select name="mode"><option value="off" ${app.mode==="off"?"selected":""}>Nicht eingerichtet</option><option value="app" ${app.mode==="app"?"selected":""}>Nur App öffnen</option>${nativeSources ? (app.mode === "link" ? '<option value="link" selected disabled>Gespeichert: Inhaltslink senden · hier nicht unterstützt</option>' : "") : `<option value="link" ${app.mode==="link"?"selected":""}>Inhaltslink senden</option>`}</select>${nativeSources && app.mode === "link" ? '<span class="small muted">Wähle für diese Geräteanbindung „Nur App öffnen“ oder „Nicht eingerichtet“. Unterstützte Favoriten verwenden ihren eigenen Titelstart.</span>' : ""}</label>
            ${provider === "netflix" ? `<label>Gewünschtes Netflix-Profil<input name="profile_name" value="${esc(app.profile_name)}" maxlength="80" placeholder="Profilname wie am TV"></label>` : ""}
          </div>
          <details class="advanced-settings provider-advanced">
            <summary>Erweiterte Einstellungen</summary>
            <div class="form-grid">
              ${nativeSources ? this._appSourceControl(target, provider, app) : `<label>App-ID<input name="app_id" value="${esc(app.app_id)}" list="app-sources-${target.id}" maxlength="160"><span class="small muted">Bei ADB der Paketname der App.</span></label>`}
              <label>Am Gerät geprüft<select name="tested">${[["untested","Ungetestet"],["app","App öffnet sich"],["title","Richtiger Titel öffnet sich"],["playback","Richtiger Inhalt spielt ab"]].map(([k,v])=>`<option value="${k}" ${app.tested===k?"selected":""}>${v}</option>`).join("")}</select></label>
            </div>
          </details>
        </details>`;
      }).join("")}
      ${nativeSources ? "" : `<datalist id="app-sources-${target.id}">${this._reportedSourceNames(target.entity_id).map(s=>`<option value="${esc(s)}">`).join("")}</datalist>`}
      </div>
    </fieldset>`;
  }

  _reportedSourceNames(entityId) {
    const reported = this.entity(entityId)?.sources;
    return [...new Set((Array.isArray(reported) && reported.length <= 512 ? reported : []).filter(source =>
      typeof source === "string" && Array.from(source).length > 0 && Array.from(source).length <= 160
      && !Array.from(source).some(character=>character !== " " && /[\p{C}\p{Z}]/u.test(character))))];
  }

  _appSourceControl(target, provider, app) {
    const reported = this.entity(target.entity_id)?.sources;
    const unreadable = reported != null && (!Array.isArray(reported) || reported.length > 512);
    const sources = this._reportedSourceNames(target.entity_id);
    const matches = sources.filter(source => appArtwork(source).provider === provider);
    const manual = app.source_mode === "manual" || (!app.source_mode && Boolean(app.app_id));
    const selected = manual ? app.app_id || "" : "";
    const automatic = matches.length === 1 ? `Automatisch erkennen · ${matches[0]}` : "Automatisch erkennen";
    const message = selected
      ? (sources.includes(selected) ? "Diese Quelle wurde ausdrücklich ausgewählt." : "Gespeicherte Quelle; vom Gerät derzeit nicht gemeldet.")
      : unreadable ? "Die Quellenliste ist derzeit nicht auswertbar. Du kannst speichern und die Geräteverbindung vor dem Appstart prüfen."
      : matches.length === 1 ? `Zugeordnet: ${matches[0]}. Der aktuelle Name wird beim Appstart vom gewählten Gerät übernommen.`
      : matches.length > 1 ? "Mehrere passende Quellen werden gemeldet. Wähle hier die gewünschte Quelle aus."
      : "Noch keine passende Quelle gemeldet. Du kannst speichern und den Fernseher vor dem Appstart einschalten.";
    const hintId = `source-hint-${target.id}-${provider}`;
    return `<label>App-Zuordnung<select name="app_id" data-source-choice aria-describedby="${esc(hintId)}"><option value="" ${!selected ? "selected" : ""}>${esc(automatic)}</option>${selected && !sources.includes(selected) ? `<option value="${esc(selected)}" selected>${esc(selected)} · gespeichert, derzeit nicht gemeldet</option>` : ""}${sources.map(source=>`<option value="${esc(source)}" ${source===selected ? "selected" : ""}>${esc(source)}</option>`).join("")}</select><span id="${esc(hintId)}" class="small muted" data-source-status role="status">${esc(message)}</span></label>`;
  }

  _refreshTargetForm(field, focusName) {
    const openDetails = [...field.querySelectorAll("details")].map(item=>item.open);
    const wasNative = Boolean(field.querySelector("[data-source-choice]"));
    this.roomDraft = this._collectRoom();
    const target = this.roomDraft.targets.find(item=>item.id === field.dataset.target);
    const ent = this.entity(target.entity_id);
    if (focusName === "entity_id" && ["samsungtv","androidtv","androidtv_remote"].includes(ent?.platform)) target.profile = ent.platform;
    const native = ["samsungtv","generic"].includes(target.profile);
    if (native !== wasNative) {
      for (const [provider, app] of Object.entries(target.apps)) {
        app.app_id = !native && target.profile === "androidtv" ? this.data.providers[provider]?.package || "" : "";
        if (native) app.source_mode = "auto";
        else delete app.source_mode;
      }
    }
    if (["androidtv","androidtv_remote"].includes(target.profile)) {
      for (const [provider, app] of Object.entries(target.apps)) {
        delete app.source_mode;
        if (target.profile === "androidtv" && !app.app_id) app.app_id = this.data.providers[provider]?.package || "";
      }
    }
    const fragment = document.createElement("template");
    fragment.innerHTML = this._targetForm(target);
    const replacement = fragment.content.firstElementChild;
    [...replacement.querySelectorAll("details")].forEach((item,index)=>{ item.open = openDetails[index] ?? item.open; });
    field.replaceWith(replacement);
    replacement.querySelector(`[name="${focusName}"]`)?.focus();
  }

  _collectRoom() {
    const form = this.shadowRoot.querySelector("#room-form");
    const value = name => form.elements.namedItem(name).value;
    return {id:this.roomDraft.id,name:value("name"),tv_entity_id:value("tv_entity_id"),prime_profile_delay:Number(value("prime_profile_delay")),prime_continue_delay:Number(value("prime_continue_delay")),targets:[...form.querySelectorAll("fieldset[data-target]")].map(field => {
      const val = name => field.querySelector(`[name="${name}"]`).value;
      return {id:field.dataset.target,name:val("target_name"),entity_id:val("entity_id"),profile:val("profile"),remote_entity_id:val("remote_entity_id"),hdmi_source:val("hdmi_source"),apps:Object.fromEntries([...field.querySelectorAll("[data-provider]")].map(app=>{
        const values=Object.fromEntries(["mode","app_id","tested"].map(key=>[key,app.querySelector(`[name="${key}"]`).value]));
        if (["samsungtv","generic"].includes(val("profile"))) values.source_mode = values.app_id ? "manual" : "auto";
        if (app.dataset.provider === "netflix") values.profile_name=app.querySelector('[name="profile_name"]').value;
        return [app.dataset.provider,values];
      }))};
    })};
  }

  async _submit(event) {
    event.preventDefault();
    const form = event.target;
    if (form.id === "favorite-form" && this.imageUpload) {
      this._uploadStatus("Bitte warte, bis das Bild hochgeladen ist.");
      return;
    }
    if (this.pending || !form.reportValidity()) return;
    const buttons = [...form.querySelectorAll("button")];
    buttons.forEach(b=>{ b.disabled=true; });
    try {
      if (form.id === "favorite-form") {
        const data = Object.fromEntries(new FormData(form));
        data.prime_auto_profile = form.elements.prime_auto_profile.checked && !form.elements.prime_auto_profile.disabled;
        data.prime_auto_continue = form.elements.prime_auto_continue.checked && !form.elements.prime_auto_continue.disabled;
        await this._command("save_favorite",{data,revision:this.editRevision,...(this.editFavoriteId?{item_id:this.editFavoriteId}:{})});
      } else {
        const data = this._collectRoom();
        await this._command("save_room",{data,item_id:data.id,revision:this.editRevision});
      }
      this._closeForm();
      this._notify("Gespeichert.");
    } catch (err) {
      this.shadowRoot.querySelector("#form-error").textContent = err.message;
    } finally { buttons.forEach(b=>{ b.disabled=false; }); }
  }

  async _click(event) {
    const button = event.target.closest("button[data-op]");
    if (!button || button.disabled) return;
    const op = button.dataset.op;
    try {
      if (op === "menu") this.dispatchEvent(new Event("hass-toggle-menu",{bubbles:true,composed:true}));
      else if (op === "rail-scroll") {
        const rail = button.closest(".app-shelf")?.querySelector("[data-rail]");
        if (rail) rail.scrollBy({
          left: (button.dataset.direction === "next" ? 1 : -1) * Math.max(rail.clientWidth * 0.8, 160),
          behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
        });
      }
      else if (op === "view") { this.view = button.dataset.view; this._render(); }
      else if (op === "reload") { this._disconnect(); await this._connect(); }
      else if (op === "pair-youtube") await this._pairYoutube();
      else if (op === "diagnostic-open") this._diagnosticForm();
      else if (op === "standard-collect") await this._collectStandardDiagnostic();
      else if (op === "diagnostic-collect") await this._collectDiagnostic();
      else if (op === "diagnostic-download") this._downloadDiagnostic();
      else if (op.startsWith("assistant-")) {
        const action = {"assistant-bind":"bind","assistant-pair":"pair","assistant-plan":"plan","assistant-run":"run","assistant-record":"record","assistant-save-profile":"save_profile","assistant-cancel":"cancel"}[op];
        if (action) await this._assistantAction(action);
      }
      else if (op === "manage-apps") await this._loadInstalledApps();
      else if (op === "review-uninstall") this._reviewUninstall();
      else if (op === "confirm-uninstall") {
        if (this.pending) return;
        const {args, deviceRef, packageId} = this.appManagement;
        button.disabled = true;
        await this._command("uninstall_app", {...args, device_ref: deviceRef, package: packageId});
        this._closeForm();
        this._notify("App im Hauptnutzer entfernt und Paketliste geprüft.");
      }
      else if (op === "image-choose") this.shadowRoot.querySelector("#favorite-image-file")?.click();
      else if (op === "image-remove") {
        this._cancelImageUpload();
        this._setFavoriteImage("");
        this._uploadStatus("");
      }
      else if (op === "favorite-new") this._favoriteForm();
      else if (op === "favorite-edit") this._favoriteForm(this.data.favorites.find(f=>f.id===button.dataset.id));
      else if (op === "room-new") this._roomForm();
      else if (op === "room-edit") this._roomForm(this.data.rooms.find(r=>r.id===button.dataset.id));
      else if (op === "dialog-close") { if (!this.pending || this.diagnostic) this._closeForm(); }
      else if (op === "target-add") {
        this.roomDraft = this._collectRoom();
        if (this.roomDraft.targets.length >= 12) throw new Error("Maximal zwölf Wiedergabeziele pro Raum.");
        const target = {id:id(),name:"",entity_id:"",profile:"generic",remote_entity_id:"",hdmi_source:"",apps:{}};
        this.shadowRoot.querySelector("#targets-form").insertAdjacentHTML("beforeend",this._targetForm(target));
      } else if (op === "target-remove") button.closest("fieldset").remove();
      else if (op === "favorite-delete" || op === "room-delete") {
        const kind = op === "room-delete" ? "room" : "favorite";
        const item = this.data[kind === "room" ? "rooms" : "favorites"].find(x=>x.id===button.dataset.id);
        this._confirmDelete(kind,item);
      } else if (op === "delete-confirm") {
        await this._command(`delete_${this.deleting.kind}`,{item_id:this.deleting.item.id,revision:this.deleting.revision});
        this._closeForm(); this._notify("Gelöscht.");
      } else if (op === "start") await this._command("start",{room_id:this.roomId,target_id:this.targetId,favorite_id:button.dataset.id});
      else if (op === "app") await this._command("app",{room_id:this.roomId,target_id:this.targetId,provider:button.dataset.provider});
      else if (op === "netflix-prepare") await this._command("prepare_netflix",{room_id:this.roomId,target_id:this.targetId});
      else if (op === "prime-continue") await this._command("control",{room_id:this.roomId,target_id:this.targetId,action:"prime_continue",value:button.dataset.job});
      else if (op === "confirm-profile") await this._command("confirm_profile",{room_id:this.roomId,target_id:this.targetId,job_id:button.dataset.job});
      else if (op === "tv-app") {
        if (this.blocked || active(this.job) || this.target?.entity_id !== this.room?.tv_entity_id || !this.target?.capabilities?.source) return;
        if (button.dataset.source === "Netflix") {
          await this._command("prepare_netflix",{room_id:this.roomId,target_id:this.targetId});
          return;
        }
        await this._command("control",{room_id:this.roomId,target_id:this.targetId,action:"source",value:button.dataset.source});
        this._notify("App-Aufruf an den Fernseher gesendet.");
      }
      else if (op === "cancel") await this._command("cancel",{room_id:this.roomId,target_id:this.targetId});
      else if (op === "control") await this._command("control",{room_id:this.roomId,target_id:this.targetId,action:button.dataset.action});
    } catch (err) {
      const error = this.shadowRoot.querySelector("dialog[open] #form-error");
      if (error) error.textContent = err.message;
      else this._notify(err.message,true);
    }
  }

  _confirmDelete(kind,item) {
    this.deleting = {kind,item,revision:this.editRevision};
    this._openForm(`${this._formHeader("Eintrag löschen")}<p>„${esc(item.name)}“ ${kind === "room" ? "mit allen Gerätezuordnungen" : "aus den Favoriten"} löschen?</p><p class="muted">Die vorhandenen Home-Assistant-Geräte bleiben erhalten.</p><footer><button data-op="dialog-close">Abbrechen</button><button class="danger" data-op="delete-confirm">Endgültig löschen</button></footer>`);
  }

  _change(event) {
    const el = event.target;
    if (el.id === "standard-system") {
      const state = this.diagnostic;
      if (state && !state.pending && Object.hasOwn(standardSystems,el.value)) {
        state.standardSystem = el.value;
        state.standardReport = null;
        state.reportMode = "standard";
        state.error = "";
        state.message = "";
        this._renderDiagnosticForm();
        this.shadowRoot.querySelector("#standard-system")?.focus();
      }
      return;
    }
    if (el.id?.startsWith("diagnostic-")) { this._diagnosticChanged(el); return; }
    if (el.id?.startsWith("assistant-")) { this._assistantChanged(el); return; }
    if (el.id === "room-select") { this.roomId=el.value; this.targetId=null; this._selectDefaults(); this._remember(); this._render(); }
    else if (el.id === "target-select") { this.targetId=el.value; this._remember(); this._render(); }
    else if (el.id === "provider-filter") { this.filter=el.value; this._render(); }
    else if (el.id === "tv-source" && el.value) this._command("control",{room_id:this.roomId,target_id:this.targetId,action:"source",value:el.value}).catch(err=>this._notify(err.message,true));
    else if (el.id === "draft-tv") {
      this.roomDraft.tv_entity_id = el.value;
      for (const list of this.shadowRoot.querySelectorAll('datalist[id^="tv-sources"]')) list.innerHTML = this._reportedSourceNames(el.value).map(s=>`<option value="${esc(s)}">`).join("");
    } else if (el.name === "mode") {
      el.closest("[data-provider]").querySelector('[name="tested"]').value="untested";
    } else if (["entity_id","profile"].includes(el.name) && el.closest("fieldset[data-target]")) {
      this._refreshTargetForm(el.closest("fieldset[data-target]"), el.name);
    } else if (el.matches?.("select[data-source-choice]")) {
      const field = el.closest("fieldset[data-target]"), provider = el.closest("[data-provider]").dataset.provider;
      const draft = this._collectRoom().targets.find(item=>item.id === field.dataset.target);
      const template = document.createElement("template");
      template.innerHTML = this._appSourceControl(draft, provider, draft.apps[provider]);
      el.closest("label").querySelector("[data-source-status]").textContent = template.content.querySelector("[data-source-status]").textContent;
    }
  }
  _input(event) {
    if (event.target.id === "favorite-search") { this.query=event.target.value; this._render(); }
    if (["standard-host","standard-extra-app"].includes(event.target.id) && this.diagnostic && !this.diagnostic.pending) {
      const state = this.diagnostic;
      if (event.target.id === "standard-host") {
        state.standardHost = event.target.value;
      } else state.standardExtra = event.target.value;
      state.standardReport = null;
      state.reportMode = "standard";
      state.error = "";
      state.message = "";
      this.shadowRoot.querySelector(".diagnostic-result")?.remove();
      this.shadowRoot.querySelector(".diagnostic-limitations")?.remove();
      this.shadowRoot.querySelector('[data-op="diagnostic-download"]')?.remove();
      const status = this.shadowRoot.querySelector("#diagnostic-status");
      if (status) status.textContent = "Starte die Standarddiagnose für diese Angaben.";
      const start = this.shadowRoot.querySelector('[data-op="standard-collect"]');
      if (start) start.disabled = this.pending || !state.standardHost.trim();
      return;
    }
    const key = {"assistant-host":"host","assistant-firmware":"firmware","assistant-app-version":"app_version"}[event.target.id];
    if (key && this.diagnostic && !this.diagnostic.pending) {
      this.diagnostic.bindingDraft[key] = event.target.value.trim();
      this.diagnostic.bindingDirty = true;
      const save = this.shadowRoot.querySelector('[data-op="assistant-bind"]');
      if (save) save.disabled = this.pending || this.diagnostic.pending || !this.diagnostic.bindingDraft.host.trim();
    }
  }
}

if (!customElements.get("cinema-control-panel")) customElements.define("cinema-control-panel", CinemaControlPanel);
