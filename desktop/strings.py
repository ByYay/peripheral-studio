"""Native desktop texts; the web UI keeps its own translations in frontend/src/i18n.ts."""

TEXTS = {
    "en": {
        "title.simulation": " — Simulation",
        "tray.show": "Show",
        "tray.hide": "Hide",
        "tray.startup": "Start with Windows",
        "tray.quit": "Quit",
        "battery.title": "Low battery",
        "battery.message": "{name}: {level}% battery remaining.",
        "script.title": "Review Python script — approval permits local code execution",
        "script.header": "Path: {path}\r\nSHA256: {sha256}\r\nReview the complete content before allowing execution.",
        "script.cancel": "Cancel",
        "script.approve": "Approve script",
        "error.title": "Unified Peripheral Manager — Startup failed",
        "error.mode": "Mode must be hardware or simulation.",
        "error.platform": "The desktop shell requires Windows and Microsoft Edge WebView2 Runtime.",
        "error.frontend": "Built frontend not found. In frontend, run npm install and npm run build, "
                          "then run python -m desktop from the project root.",
        "error.dependencies": 'Desktop dependencies are unavailable. Run python -m pip install -e ".[desktop]" '
                              "and install Microsoft Edge WebView2 Runtime.",
        "error.startup": "Desktop startup failed ({kind}). Check the frontend build, "
                         "writable app-data directory and Microsoft Edge WebView2 Runtime.",
    },
    "tr": {
        "title.simulation": " — Simülasyon",
        "tray.show": "Göster",
        "tray.hide": "Gizle",
        "tray.startup": "Windows ile başlat",
        "tray.quit": "Çıkış",
        "battery.title": "Düşük pil",
        "battery.message": "{name}: kalan pil %{level}.",
        "script.title": "Python betiğini inceleyin — onay, yerel kod çalıştırılmasına izin verir",
        "script.header": "Yol: {path}\r\nSHA256: {sha256}\r\nÇalıştırmaya izin vermeden önce içeriğin tamamını inceleyin.",
        "script.cancel": "İptal",
        "script.approve": "Betiği onayla",
        "error.title": "Unified Peripheral Manager — Başlatılamadı",
        "error.mode": "Mod hardware veya simulation olmalı.",
        "error.platform": "Masaüstü uygulaması Windows ve Microsoft Edge WebView2 Runtime gerektirir.",
        "error.frontend": "Derlenmiş arayüz bulunamadı. frontend klasöründe npm install ve npm run build "
                          "çalıştırın, ardından proje kökünden python -m desktop komutunu çalıştırın.",
        "error.dependencies": 'Masaüstü bağımlılıkları eksik. python -m pip install -e ".[desktop]" çalıştırın '
                              "ve Microsoft Edge WebView2 Runtime'ı yükleyin.",
        "error.startup": "Masaüstü uygulaması başlatılamadı ({kind}). Arayüz derlemesini, yazılabilir "
                         "uygulama verisi klasörünü ve Microsoft Edge WebView2 Runtime'ı kontrol edin.",
    },
}


def text(language, key, **values):
    return TEXTS.get(language, TEXTS["en"])[key].format(**values)
