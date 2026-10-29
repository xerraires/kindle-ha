-- KOReader user patch: keep the sleep screen up to date from a web endpoint.
--
-- Fetches an image over HTTPS whenever the device suspends, and additionally
-- on scheduled RTC wakeups (hourly, within a configurable daytime window)
-- while the device sleeps - so the sleep screen always shows current data
-- (a dashboard, weather, a status board, ...). Works on jailbroken Kindles
-- running KOReader on the STOCK framework (no framework-stop hacks).
--
-- See the README for how this works and why - especially the "dance": in the
-- screenSaver power state the stock framework refuses to (re)associate Wi-Fi
-- no matter how it is asked, so the patch briefly simulates a power-button
-- press to connect, hiding the transition behind a fullscreen overlay.
--
-- Install: copy to koreader/patches/ , edit the CONFIG block, restart KOReader.

------------------------------------------------------------------ CONFIG ----

-- The image to show. Rendered as-is; match your device's panel resolution
-- for best results (e.g. 1236x1648 for a Paperwhite 11).
local IMAGE_URL = "http://IP-DE-TU-PC:8090/kindle.png"  -- TODO: pon aqui la IP/nombre de tu servidor

-- Optional: file containing one line with a secret (no spaces); sent as
-- "X-Token" header with every request. nil = endpoint needs no auth.
-- If set but the file is missing, the patch refuses to fetch (fail closed).
local TOKEN_FILE = nil -- e.g. "/mnt/us/live-sleepscreen.token"

-- Where the image is stored. Point KOReader's sleep screen at this file
-- (Screen -> Sleep screen -> "custom image or cover", see README).
local TARGET = "/mnt/us/koreader/wallpaper.png"

-- Scheduled wakeups fire at :00 marks every hour, 24 h/day (20/oct: pedido por
-- el usuario; antes solo 07:00-23:00 del reloj del Kindle y ciclos cada 30 min).
-- WAKE_HOUR_MIN/MAX se mantienen como configuración: 0..23 = toda la jornada.
local WAKE_HOUR_MIN = 0
local WAKE_HOUR_MAX = 23

-- Flag files (create/delete at the USB root, no restart needed):
local NOWAKE_FILE = "/mnt/us/live-sleepscreen.nowake" -- disables scheduled wakeups
local FASTTEST_FILE = "/mnt/us/live-sleepscreen.fasttest" -- 5-minute wakes (testing!)

-- Optional: report weekly reading minutes (from KOReader's statistics plugin)
-- to a webhook before each fetch. POSTs to SYNC_URL .. "?value=<minutes>"
-- with the X-Token header. Set SYNC_URL to enable, e.g.
-- "https://example.com/sync/reading". Minutes are summed since the start of
-- the current week (SYNC_WEEK_START, os.date wday: 1=Sunday .. 7=Saturday).
-- SYNC_LANG_PREFIXES filters by book metadata language (prefix match, e.g.
-- { "en" } or { "fr", "de" }); empty table = count all books.
local SYNC_URL = nil
local SYNC_LANG_PREFIXES = {}
local SYNC_WEEK_START = 1
local STATS_DB = "/mnt/us/koreader/settings/statistics.sqlite3"

--------------------------------------------------------------- END CONFIG ---

local Device = require("device")
local Screensaver = require("ui/screensaver")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local ImageWidget = require("ui/widget/imagewidget")
local ScreenSaverWidget = require("ui/widget/screensaverwidget")
local Blitbuffer = require("ffi/blitbuffer")
local http = require("socket.http")
local socket = require("socket")
local socketutil = require("socketutil")
local ltn12 = require("ltn12")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")

local Screen = Device.screen
local LOG = "live-sleepscreen:"

local function readToken()
    if not TOKEN_FILE then return nil end
    local f = io.open(TOKEN_FILE, "r")
    if not f then return nil end
    local token = f:read("*l")
    f:close()
    if token then token = token:gsub("%s+", "") end
    if token == "" then return nil end
    return token
end

local function authHeaders(token, extra)
    local headers = extra or {}
    if token then headers["X-Token"] = token end
    return headers
end

-- ---- optional reading statistics sync ----

local function weekStartTimestamp()
    local t = os.date("*t")
    local days_back = (t.wday - SYNC_WEEK_START) % 7
    local midnight = os.time({ year = t.year, month = t.month, day = t.day, hour = 0, min = 0, sec = 0 })
    return midnight - days_back * 86400
end

-- Sum this week's reading time (minutes, floored) of books whose metadata
-- language starts with one of SYNC_LANG_PREFIXES, from the statistics plugin
-- DB (book.language, page_stat_data.start_time/duration). nil on any problem.
local function readWeeklyMinutes()
    if lfs.attributes(STATS_DB, "mode") ~= "file" then
        logger.info(LOG, "no statistics DB at", STATS_DB)
        return nil
    end
    local ok, SQ3 = pcall(require, "lua-ljsqlite3/init")
    if not ok then
        logger.info(LOG, "lua-ljsqlite3 unavailable")
        return nil
    end
    local lang_clause = "1=1" -- empty prefix list: count all books
    if #SYNC_LANG_PREFIXES > 0 then
        local likes = {}
        for _, p in ipairs(SYNC_LANG_PREFIXES) do
            table.insert(likes, "LOWER(b.language) LIKE '" .. p:lower():gsub("'", "''") .. "%'")
        end
        lang_clause = table.concat(likes, " OR ")
    end
    local ok2, minutes = pcall(function()
        local conn = SQ3.open(STATS_DB, "ro")
        local seconds = conn:rowexec(string.format([[
            SELECT COALESCE(SUM(psd.duration), 0)
            FROM page_stat_data psd
            JOIN book b ON b.id = psd.id_book
            WHERE psd.start_time >= %d AND (%s);
        ]], weekStartTimestamp(), lang_clause))
        conn:close()
        return math.floor(tonumber(seconds or 0) / 60)
    end)
    if not ok2 then
        logger.info(LOG, "statistics query failed:", tostring(minutes))
        return nil
    end
    return minutes
end

local function syncReading(token)
    if not SYNC_URL then return end
    local minutes = readWeeklyMinutes()
    if not minutes then return end
    logger.info(LOG, "reading this week:", minutes, "min")
    if minutes <= 0 then return end
    socketutil:set_timeout(5, 10)
    local code = socket.skip(1, http.request({
        url = SYNC_URL .. "?value=" .. minutes,
        method = "POST",
        headers = authHeaders(token, { ["Content-Length"] = "0" }),
        sink = ltn12.sink.null(),
    }))
    socketutil:reset_timeout()
    logger.info(LOG, "reading sync:", tostring(code))
end

-- ---- wallpaper fetch ----

-- Lee la bateria del Kindle (la muestra el servidor en el PNG y la reporta
-- heartbeat.koplugin a Home Assistant). Primario: lipc del framework lab126;
-- fallback: sysfs. Devuelve (nivel 0-100 | nil, cargando bool).
local function getBattery()
    local lvl, charging = nil, false
    local f = io.popen("timeout -t 5 lipc-get-prop com.lab126.powerd battLevel 2>/dev/null")
    local s = f and f:read("*l")
    if f then f:close() end
    lvl = tonumber(s)
    if not lvl then
        local sys = io.open("/sys/class/power_supply/battery-0/capacity")
            or io.open("/sys/class/power_supply/battery/capacity")
        if sys then
            lvl = tonumber(sys:read("*l"))
            sys:close()
        end
    end
    local c = io.popen("timeout -t 5 lipc-get-prop com.lab126.powerd isCharging 2>/dev/null")
    local st = c and c:read("*l")
    if c then c:close() end
    if st and st:match("^1") then charging = true end
    return lvl, charging
end

-- Sincronización de reloj (fix de las actualizaciones a las xx:59):
-- el reloj del Kindle deriva respecto al host y las alarmas armadas para el
-- ":00 del Kindle" acababan disparando cuando el host marcaba xx:59. La
-- respuesta de kindle-ha.py trae X-Host-Epoch (epoch del host); corregimos
-- SOLO la desviación de minutos/segundos (redondeando delta a horas enteras),
-- sin tocar el desfase horario del Kindle (tema de zona horaria). Se hace
-- tras cada fetch, ANTES de rearmar la alarma, para que su :00 siga cayendo
-- en el :00 del host.
local CLOCK_SYNC_MIN = 20 -- deriva sub-horaria (s) que se corrige

local function syncClock(headers)
    local v = headers and (headers["x-host-epoch"] or headers["X-Host-Epoch"])
    local host_epoch = v and tonumber(v)
    if not host_epoch then
        logger.info(LOG, "clock sync: sin X-Host-Epoch, no se corrige")
        return
    end
    local now = os.time()
    local delta = host_epoch - now
    -- desviación descontando horas enteras: delta - round(delta/3600)*3600
    local residual = delta - math.floor(delta / 3600 + 0.5) * 3600
    if math.abs(residual) < CLOCK_SYNC_MIN then
        logger.info(LOG, string.format("clock ok (delta %d s)", delta))
        return
    end
    local target = now + residual
    -- aplicar (date -s; busybox) y verificar leyendo el reloj justo después
    local function check(fmt, arg)
        local out = io.popen(string.format('date -s ' .. fmt ..
            ' >/dev/null 2>&1; date +%%s', arg))
        local s = out and out:read("*l")
        if out then out:close() end
        local applied = tonumber(s)
        return applied and math.abs(applied - target) <= 2
    end
    local ok = check('"@%d"', target)
    if not ok then
        -- fallback: date -s "YYYY-MM-DD HH:MM:SS" (hora local)
        ok = check('"%s"', os.date("%Y-%m-%d %H:%M:%S", target))
    end
    if ok then
        logger.info(LOG, string.format(
            "clock synced: delta %d s, corregidos %d s", delta, residual))
    else
        logger.info(LOG, "clock sync: no pude fijar la hora (date -s)")
    end
end

local function fetchWallpaper(token)
    local lvl, charging = getBattery()
    local url = IMAGE_URL
    if lvl then
        url = url .. "?battery=" .. lvl .. "&charging=" .. (charging and 1 or 0)
    end
    local tmp = TARGET .. ".part"
    local fh = io.open(tmp, "wb")
    if not fh then return end
    -- tight timeouts: this can block the sleep-screen paint
    socketutil:set_timeout(5, 10)
    -- capturamos también las cabeceras: kindle-ha.py devuelve X-Host-Epoch
    local a, b, c, e = http.request({
        url = url,
        method = "GET",
        headers = authHeaders(token),
        sink = socketutil.file_sink(fh),
    })
    socketutil:reset_timeout()
    -- el código va en el 2º valor (verificado: skip(1) tomaba el código);
    -- cabeceras en el 3º (LTN12) o 3º/4º según forma de retorno
    local code = tonumber(b) or tonumber(a)
    local headers = (tonumber(b) and c) or e
    if code == 200 then
        os.rename(tmp, TARGET)
        pcall(syncClock, headers)
        logger.info(LOG, "wallpaper updated")
    else
        pcall(function() fh:close() end) -- sink never closed it on early failure
        os.remove(tmp)
        logger.info(LOG, "fetch failed:", tostring(code))
    end
end

-- ESSID of the connected network, captured whenever we are online. Needed on
-- RTC wakes: in the screenSaver powerd state, enabling the radio does NOT
-- make the framework associate - that needs an explicit lipc
-- ensureConnection "wifi:<essid>" (what NetworkMgr:authenticateNetwork sends).
local last_essid

local function rememberEssid()
    if not NetworkMgr.getCurrentNetwork then return end
    local ok, nw = pcall(NetworkMgr.getCurrentNetwork, NetworkMgr)
    local ssid = ok and nw and (nw.ssid or nw.essid) or nil
    if ssid and ssid ~= "" then
        last_essid = ssid
        -- don't write the full network name into crash.log (users paste logs
        -- into bug reports)
        logger.info(LOG, "remembered ESSID", ssid:sub(1, 2) .. "…")
    end
end

local function requestAssociation()
    if not NetworkMgr.authenticateNetwork then return end
    if not last_essid then
        rememberEssid() -- unlikely to work while asleep, but try
        if not last_essid then
            logger.info(LOG, "no known ESSID, cannot request association")
            return
        end
    end
    local ok, err = pcall(NetworkMgr.authenticateNetwork, NetworkMgr, { ssid = last_essid })
    logger.info(LOG, "association requested for", last_essid:sub(1, 2) .. "…", ok and "" or tostring(err))
end

local function refresh()
    local token = readToken()
    if TOKEN_FILE and not token then
        logger.info(LOG, "no token file at", TOKEN_FILE)
        return
    end
    if not (NetworkMgr:isWifiOn() and NetworkMgr:isConnected()) then
        logger.info(LOG, "Wi-Fi down, keeping previous wallpaper")
        return
    end
    rememberEssid()
    pcall(syncReading, token)
    fetchWallpaper(token)
end

-- ---- scheduled RTC refresh while asleep ----

local overlay -- widget shown over the sleep screen after an RTC refresh
local wakeupCallback -- forward declaration: identity doubles as the cancel handle

-- Seconds until the next hour mark (:00). Runs 24 h/day since the window is
-- [0, 23]. Keeps the powerd safety margin: an alarm closer than 120 s may be
-- rejected when powerd confirms it (~10 s), so we hop a full hour instead.
local function nextWakeSeconds()
    if lfs.attributes(FASTTEST_FILE, "mode") == "file" then
        return 300
    end
    local now = os.time()
    local t = os.date("*t", now)
    -- siguiente marca en punto (:00)
    local target = os.time({ year = t.year, month = t.month, day = t.day, hour = t.hour + 1, min = 0, sec = 0 })
    -- la alarma debe seguir en el futuro cuando powerd la confirma (~10 s);
    -- demasiado cerca -> sumar una hora completa
    if target - now < 120 then
        target = target + 3600
    end
    -- ventana 0..23: siempre valida (24 h). Si alguien la estrecha, respeto
    -- el comportamiento original de saltar a WAKE_HOUR_MIN del día siguiente.
    local tt = os.date("*t", target)
    if tt.hour < WAKE_HOUR_MIN or tt.hour > WAKE_HOUR_MAX then
        local day = tt.day
        if tt.hour > WAKE_HOUR_MAX then day = day + 1 end
        target = os.time({ year = tt.year, month = tt.month, day = day, hour = WAKE_HOUR_MIN, min = 0, sec = 0 })
    end
    return target - now
end

local function scheduleWakeup()
    if not Device.wakeup_mgr then
        logger.info(LOG, "no wakeup_mgr on this device, scheduled refresh disabled")
        return
    end
    -- drop any alarm armed before the flag appeared, then honour the flag
    Device.wakeup_mgr:removeTasks(nil, wakeupCallback)
    if lfs.attributes(NOWAKE_FILE, "mode") == "file" then
        logger.info(LOG, "nowake flag present, scheduled refresh disabled")
        return
    end
    local secs = nextWakeSeconds()
    Device.wakeup_mgr:addTask(secs, wakeupCallback)
    logger.info(LOG, "next scheduled refresh in", secs, "s")
end

-- Redraw the sleep screen with the freshly fetched image: a full-screen image
-- widget shown over the existing screensaver (the pattern zmanim.koplugin uses).
-- refresh_mode: "full" (flashing, default - proper for a changed image) or
-- "ui" (non-flashing - for the dance overlay, whose content is identical to
-- what is already on screen).
local function redrawSleepScreen(refresh_mode)
    if overlay then
        UIManager:close(overlay)
        overlay = nil
    end
    local image = ImageWidget:new({
        file = TARGET,
        file_do_cache = false,
        alpha = true,
        width = Screen:getWidth(),
        height = Screen:getHeight(),
        scale_factor = 0,
    })
    overlay = ScreenSaverWidget:new({
        widget = image,
        background = Blitbuffer.COLOR_WHITE,
        covers_fullscreen = true,
    })
    overlay.modal = true
    overlay.dithered = true
    UIManager:show(overlay, refresh_mode or "full")
    logger.info(LOG, "sleep screen redrawn (" .. (refresh_mode or "full") .. ")")
end

-- One RTC refresh cycle. A Lua error anywhere -> log, do NOT re-arm (the next
-- user suspend re-arms); transient network trouble -> keep old image, re-arm.
local function runCycle()
    local ok, err = pcall(refresh)
    if not ok then
        logger.info(LOG, "refresh error, not rescheduling:", tostring(err))
        return
    end
    local ok2, err2 = pcall(redrawSleepScreen)
    if not ok2 then
        logger.info(LOG, "redraw error, not rescheduling:", tostring(err2))
        return
    end
    scheduleWakeup()
end

-- The stock framework's connection manager refuses to associate while powerd
-- is in the screenSaver state (verified: ensureConnection, radio cycling,
-- wpa_cli reassociate and a manual udhcpc lease request all fail; the same
-- request connects in ~3 s when active). Last resorts, in order: wpa_cli +
-- udhcpc straight at the supplicant (quiet - works on some devices), then
-- the "dance": simulate a power-button press so the device goes active
-- (Wi-Fi connects there), re-suspend ~2 s after connectivity; the suspend-
-- time refresh then runs with working Wi-Fi and re-arms the alarm.
local dance = false
local dance_polls = 0
local dance_started = 0
local danceResuspend
danceResuspend = function()
    if not dance then return end
    -- a timer chain revived after a suspend must never act: it would
    -- re-suspend the device right after the user wakes it
    if os.time() - dance_started > 60 then
        dance = false
        logger.info(LOG, "dance: stale timer, aborting")
        return
    end
    dance_polls = dance_polls + 1
    local state = Device.powerd.getPowerdState and Device.powerd:getPowerdState() or nil
    if state == "active" and NetworkMgr:isConnected() then
        dance = false
        logger.info(LOG, "dance: Wi-Fi up after", dance_polls, "s awake, re-suspending")
        UIManager:scheduleIn(2, function() Device.powerd:toggleSuspend() end)
        return
    end
    if dance_polls >= 20 then
        dance = false
        logger.info(LOG, "dance: timeout (state:", tostring(state), "), re-suspending anyway")
        if state == "active" then Device.powerd:toggleSuspend() end
        return
    end
    UIManager:scheduleIn(1, danceResuspend)
end

local function tryWpaCli(cmd)
    local f = io.popen("which wpa_cli 2>/dev/null")
    local path = f and f:read("*l") or nil
    if f then f:close() end
    if not path or path == "" then
        logger.info(LOG, "wpa_cli not available")
        return
    end
    logger.info(LOG, "wpa_cli", cmd, "via", path)
    os.execute(path .. " -i wlan0 " .. cmd .. " >/dev/null 2>&1")
end

-- wpa_cli can associate at L2, but nothing hands out an IP in screenSaver
-- state - so ask busybox udhcpc for a lease ourselves (backgrounded).
local function tryUdhcpc()
    local f = io.popen("which udhcpc 2>/dev/null")
    local path = f and f:read("*l") or nil
    if f then f:close() end
    if not path or path == "" then
        logger.info(LOG, "udhcpc not available")
        return
    end
    logger.info(LOG, "requesting DHCP lease via", path)
    os.execute(path .. " -i wlan0 -n -q -t 2 -T 2 >/dev/null 2>&1 &")
end

local wifi_tries = 0
local waitForWifi
waitForWifi = function()
    local wifi_on = NetworkMgr:isWifiOn()
    local connected = NetworkMgr:isConnected()
    if wifi_on and connected then
        logger.info(LOG, "Wi-Fi up after", wifi_tries, "s")
        runCycle()
        return
    end
    wifi_tries = wifi_tries + 1
    if wifi_tries % 5 == 0 then
        logger.info(LOG, "wifi wait", wifi_tries, "s, radio:", tostring(wifi_on), "connected:", tostring(connected))
    end
    -- escalation ladder, seconds hardcoded (see comment above danceResuspend)
    if wifi_tries == 4 then
        tryWpaCli("reassociate")
    elseif wifi_tries == 7 then
        tryUdhcpc()
    elseif wifi_tries >= 12 then
        logger.info(LOG, "quiet methods failed - brief wake to connect (dance)")
        dance = true
        dance_polls = 0
        dance_started = os.time()
        -- keep showing the wallpaper instead of the book while awake;
        -- "ui" = no flash, the content is identical to the current screen
        pcall(redrawSleepScreen, "ui")
        UIManager:scheduleIn(1, danceResuspend)
        Device.powerd:toggleSuspend()
        -- no light flash at night (frontlight restores on resume)
        UIManager:scheduleIn(1, function()
            pcall(function() Device.powerd:turnOffFrontlight() end)
        end)
        return -- the re-suspend runs the normal refresh and re-arms the alarm
    end
    UIManager:scheduleIn(1, waitForWifi)
end

wakeupCallback = function()
    local ok, err = pcall(function()
        logger.info(LOG, "RTC wake cycle")
        if lfs.attributes(NOWAKE_FILE, "mode") == "file" then
            logger.info(LOG, "nowake flag present, not rescheduling")
            return
        end
        wifi_tries = 0
        if NetworkMgr:isWifiOn() and NetworkMgr:isConnected() then
            runCycle()
        else
            NetworkMgr:restoreWifiAsync()
            requestAssociation()
            UIManager:scheduleIn(1, waitForWifi)
        end
    end)
    if not ok then
        logger.info(LOG, "wake cycle error, not rescheduling:", tostring(err))
    end
end

-- Start Wi-Fi as early as possible on an RTC wake: our callback only runs
-- ~15 s after wakeup (KindlePowerD:checkUnexpectedWakeup), so kicking the
-- radio at wakeupFromSuspend gives association a head start. Guarded to
-- wakes near our alarm time (random framework wakes happen several times a
-- day and shouldn't cost a Wi-Fi power-up) while the device stays asleep.
if Device.wakeup_mgr and Device.powerd and Device.powerd.wakeupFromSuspend then
    local orig_wakeup = Device.powerd.wakeupFromSuspend
    -- N.º de serie de la despertada actual (invalida watchdogs antiguos) e instantes
    -- de alarma armada y del último ciclo ejecutado (para autocuración y antirrebote).
    local wake_serial = 0
    local armed_epoch = nil
    local last_cycle_ts = 0
    local orig_scheduleWakeup = scheduleWakeup
    scheduleWakeup = function(...)
        -- recordar el instante de la alarma que acabamos de armar (para el watchdog)
        local ok, now = pcall(os.time)
        if ok then
            armed_epoch = now + nextWakeSeconds()
        end
        return orig_scheduleWakeup(...)
    end
    Device.powerd.wakeupFromSuspend = function(self, ...)
        pcall(function()
            if not Device.wakeup_mgr:isWakeupAlarmScheduled() then return end
            local task = Device.wakeup_mgr._task_queue and Device.wakeup_mgr._task_queue[1]
            -- 90 s matches KOReader's own wakeupAction proximity window
            if task and task.epoch and math.abs(os.time() - task.epoch) > 90 then return end
            wake_serial = wake_serial + 1
            local my_serial = wake_serial
            local state = self.getPowerdState and self:getPowerdState() or nil
            if state == "screenSaver" or state == "suspended" then
                logger.info(LOG, "early Wi-Fi restore on RTC wake")
                NetworkMgr:restoreWifiAsync()
                -- give the radio a moment, then explicitly ask for association
                UIManager:scheduleIn(2, requestAssociation)
                -- WATCHDOG (autocuración, 19/oct): KOReader decide a los ~15 s si fue "scheduled"
                -- u "unscheduled" (checkUnexpectedWakeup). El 19/oct ~13:00 (12:00 del reloj del
                -- Kindle) despertó la alarma RTC, restauramos WiFi y a los 15 s KOReader la dio
                -- por "unscheduled" (el flag/cola de wakeup_mgr no coincidía: ver RTC._wakeup_scheduled
                -- que RTC:setWakeupAlarm pone a false si el ioctl falla) → el ciclo NO corrió y el
                -- salvapantallas quedó viejo. Si 25 s tras la despertada el ciclo sigue sin ejecutarse
                -- y estamos en la ventana de nuestra alarma, lo ejecutamos nosotros (y runCycle rearma).
                UIManager:scheduleIn(25, function()
                    if my_serial ~= wake_serial then return end -- despertada más reciente, abortar
                    local st = Device.powerd.getPowerdState
                        and Device.powerd:getPowerdState() or nil
                    if st ~= "screenSaver" and st ~= "suspended" then return end -- despertada por usuario
                    if os.time() - last_cycle_ts < 120 then return end -- ya corrió (KOReader o nosotros)
                    if not armed_epoch then return end
                    local due = os.time() >= armed_epoch - 5 and os.time() <= armed_epoch + 240
                    if not due then return end -- despertada aleatoria del framework, no es nuestra
                    logger.info(LOG, "watchdog: KOReader no ejecutó el ciclo programado, ejecutándolo")
                    wakeupCallback()
                end)
            end
        end)
        return orig_wakeup(self, ...)
    end
    -- envolver wakeupCallback para registrar cuándo corrió el último ciclo
    local orig_wakeupCallback = wakeupCallback
    wakeupCallback = function(...)
        last_cycle_ts = os.time()
        return orig_wakeupCallback(...)
    end
end

-- Power events (sleep/wake/power button) reach KOReader through a helper
-- process (lipc-wait-event behind the input module's "fake_events" pipe). If
-- that helper dies, every input poll returns EPIPE: KOReader busy-loops and
-- never sees the wake event, so the sleep screen stays up until a restart.
-- Seen once in ~180 wake cycles, cause unknown. Swap the dead pipe for a
-- fresh helper instead. If that keeps failing (5 revivals within 5 min),
-- restart KOReader as a last resort - never leave the device frozen.
local EPIPE, EINTR = 32, 4
local input_mod = Device.input and Device.input.input
if Device:isKindle() and input_mod and input_mod.waitForEvent and not input_mod.is_ffi then
    local revivals = {}
    local restarting = false
    local function reviveHelper()
        local now = os.time()
        while revivals[1] and now - revivals[1] > 300 do table.remove(revivals, 1) end
        if #revivals >= 5 then
            if not restarting then
                restarting = true
                logger.warn(LOG, "power-event helper keeps dying, restarting KOReader")
                -- takes effect once the EPIPE is passed through to UIManager
                UIManager:restartKOReader()
            end
            return false
        end
        table.insert(revivals, now)
        local ok, fd = pcall(function()
            Device.input:close("fake_events")
            return Device.input:open("fake_events")
        end)
        logger.warn(LOG, "power-event helper died (EPIPE), reopened:", ok, tostring(fd))
        return true
    end
    local function handle(ok, ev, ...)
        -- EINTR makes Input:waitEvent retry the poll right away
        if ok == false and ev == EPIPE and reviveHelper() then return false, EINTR end
        return ok, ev, ...
    end
    local orig_wait = input_mod.waitForEvent
    input_mod.waitForEvent = function(...)
        return handle(orig_wait(...))
    end
end

-- ---- hooks ----

local orig_setup = Screensaver.setup
Screensaver.setup = function(self, ...)
    dance = false -- a suspend ends any dance; kills revived stale timers
    -- Force KOReader to show our wallpaper as a sleep-screen image,
    -- regardless of the user's screensaver_type setting. This makes the
    -- patch self-sufficient (no manual screensaver config needed).
    pcall(function()
        if G_reader_settings then
            G_reader_settings:saveSetting("screensaver_type", "random_image")
            G_reader_settings:saveSetting("screensaver_dir", "/mnt/us/koreader")
            G_reader_settings:saveSetting("screensaver_show_message", false)
        end
    end)
    pcall(refresh)
    pcall(scheduleWakeup)
    return orig_setup(self, ...)
end

-- Called by Kindle:outofScreenSaver on a user wake (not on RTC wakes): drop
-- the overlay and cancel the pending scheduled refresh.
local orig_close = Screensaver.close
Screensaver.close = function(self, ...)
    -- during the dance the overlay must stay: it hides the book while the
    -- device is briefly active
    if overlay and not dance then
        UIManager:close(overlay)
        overlay = nil
    end
    if Device.wakeup_mgr then
        Device.wakeup_mgr:removeTasks(nil, wakeupCallback)
        if not dance then
            logger.info(LOG, "user wake, cancelled scheduled refresh")
        end
    end
    local result = orig_close(self, ...)
    -- Reforcecer el wake visual (20/oct): al despertar el framework de Amazon
    -- repinta su home y en e-ink quedan fantasmas/screenshots viejas hasta que
    -- el usuario toca varias veces. KOReader ya hace un setDirty("all", "full")
    -- en nextTick si el widget se cerro, pero por si acaso no llego a ejecutarse
    -- (o el framework repinta despues), lanzamos un full refresh adicional.
    pcall(function()
        UIManager:scheduleIn(0.4, function()
            UIManager:setDirty("all", "full")
        end)
    end)
    return result
end

-- Blindar las lecturas de bateria que hacen KOReader/heartbeat.koplugin a traves
-- de la API nativa lipc (KindlePowerD:getCapacityHW / isChargingHW). Si alguna
-- propiedad lipc se atasca, la llamada nativa bloquearia TODO el bucle de
-- eventos de KOReader (freeze visto con battery.chargelevel el 16/oct: 2 dias
-- congelado). Reimplementamos esas HW calls con lipc-get-prop externo +
-- timeout (busybox timeout -t SECS), que nunca cuelga.
local function __batteryLipcInt(prop, to)
    local f = io.popen(string.format("timeout -t %d lipc-get-prop com.lab126.powerd %s 2>/dev/null", to or 5, prop))
    local s = f and f:read("*l") or nil
    if f then f:close() end
    return tonumber(s)
end
pcall(function()
    local P = Device and Device.powerd
    if P and P.getCapacityHW then
        P.getCapacityHW = function(self)
            local v = __batteryLipcInt("battLevel", 5)
            if v then return v end
            return 0
        end
        P.isChargingHW = function(self)
            return (__batteryLipcInt("isCharging", 5) or 0) == 1
        end
    end
end)
