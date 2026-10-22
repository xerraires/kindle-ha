-- KOReader settings: configurar pantalla de reposo como imagen fija.
-- El parche live-sleepscreen escribe en /mnt/us/koreader/wallpaper.png y lo
-- redibuja al suspender/despertar. Usamos random_image apuntando a ese dir.
return {
    screensaver_type = "random_image",
    screensaver_dir = "/mnt/us/koreader",
    screensaver_show_message = false,
    screensaver_img_background = "white",
    screensaver_cycle_images_alphabetically = false,
    -- Red WiFi al volver de reposo (el patch necesita WiFi en reposo).
    auto_restore_wifi = true,
}
