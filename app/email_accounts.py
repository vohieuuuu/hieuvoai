EMAIL_ACCOUNTS = [
    {"email": "paolank7691b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "paulysb3221b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "pandorafq6441b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "peggienw7771b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "shirjd7431b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "roseannpg6781b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "risaak2781b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "roseannlg6351b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "paolinaom4671b111@nongbualoi.org", "password": "Anhtai777"},
    {"email": "cynthielj6071b111@nongbualoi.org", "password": "Anhtai777"},
] 

def chrome_options_func(profile_dir):
    from seleniumwire import webdriver
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-logging")
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    return options 