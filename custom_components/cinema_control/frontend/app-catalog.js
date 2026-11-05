/* Local artwork identities are independent of playback providers and TV source names. */
export const normalizeAppName = name => String(name ?? "")
  .normalize("NFKD").replace(/\p{M}/gu, "").toLocaleLowerCase("de")
  .replace(/[^\p{L}\p{N}+]/gu, "");

const catalog = [
  ["netflix", ["Netflix"]],
  ["disney", ["Disney+", "Disney", "Disney Plus", "DisneyPlus"]],
  ["prime", ["Prime Video", "Amazon Prime Video", "Amazon Video"]],
  ["apple", ["Apple TV", "Apple TV+", "Apple TV Plus"]],
  ["youtube", ["YouTube"]],
  ["plex", ["Plex"]],
  ["spotify", ["Spotify", "Spotify Music"]],
  ["ard", ["ARD", "ARD Mediathek", "Das Erste", "Das Erste Mediathek"]],
  ["zdf", ["ZDF", "ZDFmediathek", "ZDF Mediathek"]],
  ["arte", ["ARTE", "ARTE TV", "ARTE Mediathek", "ARTE+7"]],
  ["dazn", ["DAZN"]],
  ["wow", ["WOW", "WOW TV", "Sky Ticket"]],
  ["samsungtvplus", ["Samsung TV Plus", "Samsung TV+", "TV Plus", "TV+"]],
  ["twitch", ["Twitch"]],
  ["waipu", ["waipu.tv", "waipu tv", "waipu"]],
  ["pluto", ["Pluto TV", "PlutoTV"]],
  ["paramount", ["Paramount+", "Paramount Plus", "ParamountPlus"]],
  ["crunchyroll", ["Crunchyroll"]],
  ["kodi", ["Kodi"]],
  ["vimeo", ["Vimeo"]],
];

const aliases = new Map(catalog.flatMap(([brand, names]) => names.map(name => [normalizeAppName(name), brand])));
const playbackProviders = new Set(["netflix", "disney", "prime", "apple", "youtube", "plex"]);

export function appArtwork(name) {
  const key = normalizeAppName(name);
  const brand = aliases.get(key) || "";
  const displayName = String(name ?? "").normalize("NFC").trim();
  const words = displayName.match(/[\p{L}\p{N}]+/gu) || [];
  const letters = words.length > 1
    ? words.slice(0, 2).map(word => Array.from(word)[0]).join("")
    : Array.from(words[0] || "App").slice(0, 2).join("");
  let hash = 0;
  for (const character of key) hash = (Math.imul(hash, 31) + character.codePointAt(0)) >>> 0;
  const fullName = Boolean(displayName && Array.from(displayName).length <= 5 && words.length);
  const initials = fullName ? displayName : letters.toLocaleUpperCase("de");
  return {brand, provider: playbackProviders.has(brand) ? brand : "", initials, fullName, tone: hash % 6};
}
