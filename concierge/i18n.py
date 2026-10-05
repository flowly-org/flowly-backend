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
        "en": "I'm {name}, your Flowly concierge. Just write to me, e.g.\n• what's on my plate today?\n• add “call the accountant” tomorrow in Personal\n• move the proposal to Friday\n• send a photo of a written list to turn it into tasks\n• send a voice message: dictate tasks or ask anything\n• start the timer on the proposal / stop the timer\n\nCommands: /today brief · /alerts_off · /alerts_on · /undo (last photo) · /forget (clear what I remember) · /apps (download the apps, e.g. /apps android) · /help",
        "it": "Sono {name}, il tuo concierge Flowly. Scrivimi pure, ad esempio\n• cosa ho in programma oggi?\n• aggiungi “chiamare il commercialista” domani in Personal\n• sposta la proposta a venerdì\n• manda la foto di una lista scritta per trasformarla in attività\n• manda un vocale: detta attività o chiedimi qualcosa\n• avvia il timer sulla proposta / ferma il timer\n\nComandi: /today riepilogo · /alerts_off · /alerts_on · /undo (ultima foto) · /forget (dimentica ciò che ricordo) · /apps (scarica le app, es. /apps android) · /help",
        "es": "Soy {name}, tu conserje de Flowly. Escríbeme, por ejemplo\n• ¿qué tengo hoy?\n• añade “llamar al contable” mañana en Personal\n• mueve la propuesta al viernes\n• envía una foto de una lista escrita para convertirla en tareas\n• envía un mensaje de voz: dicta tareas o pregúntame lo que quieras\n• inicia el temporizador en la propuesta / para el temporizador\n\nComandos: /today resumen · /alerts_off · /alerts_on · /undo (última foto) · /forget (borrar lo que recuerdo) · /apps (descargar las apps, p. ej. /apps android) · /help",
        "fr": "Je suis {name}, ton concierge Flowly. Écris-moi, par exemple\n• qu'est-ce que j'ai aujourd'hui ?\n• ajoute « appeler le comptable » demain dans Personal\n• déplace la proposition à vendredi\n• envoie la photo d'une liste écrite pour la transformer en tâches\n• envoie un message vocal : dicte des tâches ou demande-moi quelque chose\n• lance le minuteur sur la proposition / arrête le minuteur\n\nCommandes : /today résumé · /alerts_off · /alerts_on · /undo (dernière photo) · /forget (oublier ce que je sais) · /apps (télécharger les apps, ex. /apps android) · /help",
        "de": "Ich bin {name}, dein Flowly-Concierge. Schreib mir einfach, z. B.\n• was steht heute an?\n• füge „Steuerberater anrufen“ morgen in Personal hinzu\n• verschiebe das Angebot auf Freitag\n• schick ein Foto einer geschriebenen Liste, um daraus Aufgaben zu machen\n• schick eine Sprachnachricht: diktiere Aufgaben oder frag mich etwas\n• starte den Timer für das Angebot / stoppe den Timer\n\nBefehle: /today Übersicht · /alerts_off · /alerts_on · /undo (letztes Foto) · /forget (Gemerktes löschen) · /apps (Apps herunterladen, z. B. /apps android) · /help",
        "pt": "Sou {name}, o teu concierge Flowly. Escreve-me, por exemplo\n• o que tenho hoje?\n• adiciona “ligar ao contabilista” amanhã em Personal\n• passa a proposta para sexta\n• envia a foto de uma lista escrita para a transformar em tarefas\n• envia uma mensagem de voz: dita tarefas ou pergunta-me o que quiseres\n• inicia o temporizador na proposta / para o temporizador\n\nComandos: /today resumo · /alerts_off · /alerts_on · /undo (última foto) · /forget (apagar o que lembro) · /apps (descarregar as apps, ex. /apps android) · /help",
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
    "photo_added": {
        "en": "📋 Added {n} tasks to {ws} ({when}):", "it": "📋 Ho aggiunto {n} attività a {ws} ({when}):",
        "es": "📋 He añadido {n} tareas a {ws} ({when}):", "fr": "📋 J'ai ajouté {n} tâches à {ws} ({when}) :",
        "de": "📋 {n} Aufgaben zu {ws} hinzugefügt ({when}):", "pt": "📋 Adicionei {n} tarefas a {ws} ({when}):",
    },
    "photo_undo_hint": {
        "en": "Something wrong? /undo removes them.", "it": "Qualcosa non va? /undo le rimuove.", "es": "¿Algo mal? /undo las elimina.",
        "fr": "Un souci ? /undo les supprime.", "de": "Stimmt was nicht? /undo entfernt sie.", "pt": "Algo errado? /undo remove-as.",
    },
    "photo_reading": {
        "en": "Reading your list…", "it": "Sto leggendo la lista…", "es": "Leyendo tu lista…",
        "fr": "Je lis ta liste…", "de": "Ich lese deine Liste…", "pt": "A ler a tua lista…",
    },
    "voice_none": {
        "en": "I couldn't make out any words in that voice message. Could you try again?",
        "it": "Non sono riuscito a capire il messaggio vocale. Puoi riprovare?",
        "es": "No he podido entender el mensaje de voz. ¿Puedes intentarlo de nuevo?",
        "fr": "Je n'ai pas compris le message vocal. Tu peux réessayer ?",
        "de": "Ich konnte die Sprachnachricht nicht verstehen. Versuchst du es noch einmal?",
        "pt": "Não consegui perceber a mensagem de voz. Podes tentar outra vez?",
    },
    "voice_too_long": {
        "en": "That voice message is a bit long for me: please keep it under {minutes} minutes.",
        "it": "Il vocale è un po' lungo per me: tienilo sotto i {minutes} minuti.",
        "es": "Ese mensaje de voz es algo largo para mí: mantenlo por debajo de {minutes} minutos.",
        "fr": "Ce message vocal est un peu long pour moi : reste sous les {minutes} minutes.",
        "de": "Die Sprachnachricht ist mir etwas zu lang: bitte unter {minutes} Minuten bleiben.",
        "pt": "Essa mensagem de voz é um pouco longa para mim: mantém-na abaixo de {minutes} minutos.",
    },
    "photo_none": {
        "en": "I couldn't find any to-do items in that photo. Try a sharper one, closer to the page.",
        "it": "Non ho trovato attività in quella foto. Prova con una più nitida e più vicina al foglio.",
        "es": "No encontré tareas en esa foto. Prueba con una más nítida y más cerca de la hoja.",
        "fr": "Je n'ai trouvé aucune tâche sur cette photo. Essaie une photo plus nette, plus près de la feuille.",
        "de": "Ich habe auf dem Foto keine Aufgaben gefunden. Versuch ein schärferes Foto, näher am Blatt.",
        "pt": "Não encontrei tarefas nessa foto. Tenta uma mais nítida e mais perto da folha.",
    },
    "undo_done": {
        "en": "Removed the {n} tasks from your last photo.", "it": "Ho rimosso le {n} attività della tua ultima foto.",
        "es": "He eliminado las {n} tareas de tu última foto.", "fr": "J'ai supprimé les {n} tâches de ta dernière photo.",
        "de": "Die {n} Aufgaben aus deinem letzten Foto sind entfernt.", "pt": "Removi as {n} tarefas da tua última foto.",
    },
    "undo_nothing": {
        "en": "There's nothing to undo.", "it": "Non c'è niente da annullare.", "es": "No hay nada que deshacer.",
        "fr": "Il n'y a rien à annuler.", "de": "Es gibt nichts rückgängig zu machen.", "pt": "Não há nada para desfazer.",
    },
    "when_today": {"en": "today", "it": "oggi", "es": "hoy", "fr": "aujourd'hui", "de": "heute", "pt": "hoje"},
    "when_tomorrow": {"en": "tomorrow", "it": "domani", "es": "mañana", "fr": "demain", "de": "morgen", "pt": "amanhã"},
    "when_backlog": {"en": "backlog", "it": "backlog", "es": "backlog", "fr": "backlog", "de": "Backlog", "pt": "backlog"},
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
