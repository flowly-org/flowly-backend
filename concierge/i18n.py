"""Languages the concierge can use, and translations of the bot's fixed messages."""

LANGUAGES = {
    "auto": "Same as the user's messages",
    "en": "English",
    "it": "Italiano",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "pt": "Português",
}

# Names the LLM is told to write in.
LLM_NAMES = {"en": "English", "it": "Italian", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese"}

STRINGS = {
    "help": {
        "en": "I'm {name}, your Flowly concierge. Just write to me, e.g.\n• what's on my plate today?\n• add “call the accountant” tomorrow in Personal\n• move the proposal to Friday\n\nCommands: /today brief · /alerts_off · /alerts_on · /forget (clear what I remember) · /help",
        "it": "Sono {name}, il tuo concierge Flowly. Scrivimi pure, ad esempio\n• cosa ho in programma oggi?\n• aggiungi “chiamare il commercialista” domani in Personal\n• sposta la proposta a venerdì\n\nComandi: /today riepilogo · /alerts_off · /alerts_on · /forget (dimentica ciò che ricordo) · /help",
        "es": "Soy {name}, tu conserje de Flowly. Escríbeme, por ejemplo\n• ¿qué tengo hoy?\n• añade “llamar al contable” mañana en Personal\n• mueve la propuesta al viernes\n\nComandos: /today resumen · /alerts_off · /alerts_on · /forget (borrar lo que recuerdo) · /help",
        "fr": "Je suis {name}, ton concierge Flowly. Écris-moi, par exemple\n• qu'est-ce que j'ai aujourd'hui ?\n• ajoute « appeler le comptable » demain dans Personal\n• déplace la proposition à vendredi\n\nCommandes : /today résumé · /alerts_off · /alerts_on · /forget (oublier ce que je sais) · /help",
        "de": "Ich bin {name}, dein Flowly-Concierge. Schreib mir einfach, z. B.\n• was steht heute an?\n• füge „Steuerberater anrufen“ morgen in Personal hinzu\n• verschiebe das Angebot auf Freitag\n\nBefehle: /today Übersicht · /alerts_off · /alerts_on · /forget (Gemerktes löschen) · /help",
        "pt": "Sou {name}, o teu concierge Flowly. Escreve-me, por exemplo\n• o que tenho hoje?\n• adiciona “ligar ao contabilista” amanhã em Personal\n• passa a proposta para sexta\n\nComandos: /today resumo · /alerts_off · /alerts_on · /forget (apagar o que lembro) · /help",
    },
    "connected": {
        "en": "Connected to Flowly as {user}. 🎉", "it": "Collegato a Flowly come {user}. 🎉", "es": "Conectado a Flowly como {user}. 🎉",
        "fr": "Connecté à Flowly en tant que {user}. 🎉", "de": "Mit Flowly verbunden als {user}. 🎉", "pt": "Ligado ao Flowly como {user}. 🎉",
    },
    "alerts_off": {
        "en": "Alerts paused. /alerts_on to resume.", "it": "Avvisi in pausa. /alerts_on per riattivarli.", "es": "Avisos en pausa. /alerts_on para reanudar.",
        "fr": "Alertes en pause. /alerts_on pour reprendre.", "de": "Benachrichtigungen pausiert. /alerts_on zum Fortsetzen.", "pt": "Alertas em pausa. /alerts_on para retomar.",
    },
    "alerts_on": {
        "en": "Alerts are on.", "it": "Avvisi attivi.", "es": "Avisos activados.", "fr": "Alertes activées.", "de": "Benachrichtigungen sind an.", "pt": "Alertas ativos.",
    },
    "forgot": {
        "en": "I've cleared what I remembered about you.", "it": "Ho cancellato ciò che ricordavo di te.", "es": "He borrado lo que recordaba de ti.",
        "fr": "J'ai effacé ce que je savais de toi.", "de": "Ich habe gelöscht, was ich mir über dich gemerkt hatte.", "pt": "Apaguei o que me lembrava de ti.",
    },
    "brief_prep": {
        "en": "Coming up, may need preparation", "it": "In arrivo, forse da preparare", "es": "Próximamente, quizá requiere preparación",
        "fr": "À venir, peut-être à préparer", "de": "Demnächst, braucht evtl. Vorbereitung", "pt": "Em breve, talvez precise de preparação",
    },
    "pro_only": {
        "en": "The concierge is a Flowly Pro feature.", "it": "Il concierge è una funzione di Flowly Pro.", "es": "El conserje es una función de Flowly Pro.",
        "fr": "Le concierge est une fonctionnalité Flowly Pro.", "de": "Der Concierge ist eine Flowly-Pro-Funktion.", "pt": "O concierge é uma funcionalidade Flowly Pro.",
    },
    "error": {
        "en": "Sorry, my AI provider didn't answer. Try again in a moment.", "it": "Scusa, il mio provider AI non ha risposto. Riprova tra poco.",
        "es": "Lo siento, mi proveedor de IA no respondió. Inténtalo de nuevo en un momento.", "fr": "Désolé, mon fournisseur d'IA n'a pas répondu. Réessaie dans un instant.",
        "de": "Entschuldige, mein KI-Anbieter hat nicht geantwortet. Versuch es gleich noch einmal.", "pt": "Desculpa, o meu fornecedor de IA não respondeu. Tenta daqui a pouco.",
    },
    "brief_greeting": {
        "en": "Good morning! {date}:", "it": "Buongiorno! {date}:", "es": "¡Buenos días! {date}:", "fr": "Bonjour ! {date} :", "de": "Guten Morgen! {date}:", "pt": "Bom dia! {date}:",
    },
    "brief_empty": {
        "en": "Nothing planned for today.", "it": "Niente in programma per oggi.", "es": "Nada planeado para hoy.",
        "fr": "Rien de prévu aujourd'hui.", "de": "Für heute ist nichts geplant.", "pt": "Nada planeado para hoje.",
    },
    "brief_carried": {"en": "carried over", "it": "da ieri", "es": "pendiente", "fr": "reportée", "de": "übertragen", "pt": "pendente"},
    "brief_due": {"en": "Due today", "it": "In scadenza oggi", "es": "Vence hoy", "fr": "À rendre aujourd'hui", "de": "Heute fällig", "pt": "Prazo hoje"},
}


def t(key: str, lang: str, **params) -> str:
    table = STRINGS[key]
    text = table.get(lang if lang in table else "en")
    return text.format(**params)
