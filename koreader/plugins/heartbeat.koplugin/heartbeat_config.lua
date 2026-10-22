-- Plantilla de configuracion del plugin heartbeat (bateria del Kindle -> Home Assistant).
-- Edita host y token con los de TU instalacion. Este fichero vive en:
--   /mnt/us/koreader/plugins/heartbeat.koplugin/heartbeat_config.lua
return {
    -- Home Assistant connection settings
    -- Ajusta la IP/nombre de tu Home Assistant y el puerto (8123 por defecto;
    -- si tu HA responde en otro puerto, cambialo aqui).
    host = "IP-DE-TU-HOME-ASSISTANT",
    port = 8123,
    https = false,
    -- Token de acceso de larga duracion: en HA -> Perfil -> Seguridad ->
    -- "Tokens de acceso de larga duracion" -> Crear token.
    token = "PEGA-AQUI-TU-TOKEN-DE-LARGA-DURACION",
}
-- Nota: el NOMBRE del sensor se configura en KOReader
--   (Tools -> Heartbeat Configuration -> Sensor Name), p. ej. "koreader_status"
--   -> entidad resultante: binary_sensor.koreader_status
--   Atributos que envia: battery_level, is_charging, last_seen, device_model, ...
