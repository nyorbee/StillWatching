# StillWatching v1.3.0

Big refresh of the old "Are you still watching?" tray app — rewritten almost from scratch.

## ✨ New
- Asks only after real **mouse/keyboard inactivity** (5 min – 2 h) instead of a fixed timer
- **Sound check** — asks only while the player is really playing, so a paused video is never resumed by mistake
- **Countdown with an action** when nobody answers: leave paused / close player / sleep / hibernate / shut down
- **Browsers**: YouTube, Netflix, HBO Max, Disney+, Prime Video, Player.pl, CANAL+, Twitch and more
- **Active hours** (e.g. 22:00–06:00), optional
- **Settings window** with editable lists of players, browsers and sites ("Pick from open windows")
- **English / Polish** interface
- **Update check** (GitHub releases), **Start with Windows**, single instance, log file
- Renamed PotPlayer builds (e.g. `zuikaku.exe`) detected by folder

## 🐛 Fixed (from 0.5-beta)
- Asked even while you were actively using the PC
- Space could resume an already paused video, or land in a different window
- Clicking *Start* twice opened several dialogs at once; no way to stop checking
- Changing the interval took effect only after the current wait (up to 1 h)

## 📥 Download
`StillWatching.exe` below — run it, look for the 👁️ in the tray. Settings: `%APPDATA%\StillWatching\`.

---

**PL:** Duże odświeżenie programu — pyta tylko po faktycznej bezczynności i gdy film naprawdę gra,
odliczanie z akcją (pauza / zamknij / uśpij / hibernuj / wyłącz), przeglądarki (YouTube, Netflix,
HBO Max…), godziny działania, okno ustawień, interfejs PL/EN i sprawdzanie aktualizacji.
