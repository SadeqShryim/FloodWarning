// Every word on the report page, in English, Arabic and Spanish. Arabic and Spanish are written
// to read naturally for Dearborn residents, not translated word for word.
import type { UiLanguage } from '../types'

export interface Strings {
  docTitle: string
  langShort: string // label on the switcher
  langFull: string // spoken by screen readers
  langSwitcher: string
  emergency: string

  idleTitle: string
  idleLead: string
  record: string
  recordAria: string
  recordHint: string
  typeInstead: string

  recordingTitle: string
  recordingLead: string
  allowMic: string
  stop: string
  stopAria: string
  autoStopNote: string
  cancel: string

  reviewTitle: string
  reviewLead: string
  play: string
  pause: string
  recordAgain: string
  addDetails: string
  detailsLabel: string
  send: string
  tryAgain: string

  addPhoto: string
  photoAdded: string
  removePhoto: string
  photoOptional: string

  typeTitle: string
  typeLead: string
  typeLabel: string
  typePlaceholder: string
  useVoice: string
  textEmpty: string

  sending: string
  sendingLead: string

  locating: string
  locFound: (meters: number) => string
  locUnavailable: string
  locDenied: string
  locInsecure: string
  locRetry: string
  addressLabel: string
  addressPlaceholder: string
  addressHint: string

  micDenied: string
  micNotFound: string
  micBusy: string
  micUnsupported: string
  micInsecure: string
  micFailed: string
  micEmpty: string
  typeYourReport: string

  errNetwork: string
  errTimeout: string
  errServer: string
  errTooLarge: string
  errMissing: string

  doneTitle: string
  reportNumber: (id: number) => string
  understanding: string
  savedGeneric: string
  understoodHeading: string
  chipDepth: (cm: number) => string
  chipRising: string
  chipLivingSpace: string
  chipBasement: string
  chipCar: string
  chipTrapped: string
  chipMedical: string
  chipElderly: string
  chipChildren: string
  chipDisabled: string
  chipPeople: (n: number) => string
  done911: string
  sendAnother: string

  seconds: (n: number) => string
  annRecording: string
  annStopped: (duration: string) => string
  annCancelled: string
  annSending: string
  annReceived: (id: number) => string
  annUnderstood: string
}

const inches = (cm: number) => Math.round(cm / 2.54)

const en: Strings = {
  docTitle: 'Report a flood – FloodLine',
  langShort: 'EN',
  langFull: 'English',
  langSwitcher: 'Language',
  emergency: 'In a life-threatening emergency, call 911',

  idleTitle: 'Report flooding',
  idleLead: 'Tap the red button and tell us what is happening, in any language.',
  record: 'Record',
  recordAria: 'Start recording a voice note',
  recordHint: 'Say where you are, how deep the water is, and who is with you.',
  typeInstead: 'Type instead',

  recordingTitle: 'Listening…',
  recordingLead: 'Speak now. Tap the button when you are done.',
  allowMic: 'Allow the microphone if your phone asks.',
  stop: 'Stop',
  stopAria: 'Stop recording',
  autoStopNote: 'Recording stops by itself after 1 minute.',
  cancel: 'Cancel',

  reviewTitle: 'Your voice note is ready',
  reviewLead: 'Listen to it if you like, then send it.',
  play: 'Listen',
  pause: 'Pause',
  recordAgain: 'Record again',
  addDetails: 'Add written details',
  detailsLabel: 'Written details (optional)',
  send: 'Send report',
  tryAgain: 'Try again',

  addPhoto: 'Add a photo',
  photoAdded: 'Photo added',
  removePhoto: 'Remove photo',
  photoOptional: 'Optional. A photo of the water helps responders.',

  typeTitle: 'Write your report',
  typeLead: 'Tell us what is happening, in any language.',
  typeLabel: 'What is happening?',
  typePlaceholder:
    'Example: Water is knee-deep in our basement and rising. My mother is 80 and cannot climb the stairs.',
  useVoice: 'Record a voice note instead',
  textEmpty: 'Write a few words about what is happening first.',

  sending: 'Sending your report…',
  sendingLead: 'Keep this page open.',

  locating: 'Finding your location…',
  locFound: (m) => `Location found (±${m} m)`,
  locUnavailable: 'Your location is not available.',
  locDenied: 'Location is turned off for this page.',
  locInsecure: 'Location needs the secure https link.',
  locRetry: 'Try again',
  addressLabel: 'Street and nearest cross street',
  addressPlaceholder: 'Example: Warren Ave and Schaefer Rd',
  addressHint: 'Or just say the place in your voice note.',

  micDenied: 'The microphone is blocked. Allow it in your browser settings, or type your report.',
  micNotFound: 'No microphone was found on this phone. You can type your report.',
  micBusy: 'Another app is using the microphone. Close it and try again, or type your report.',
  micUnsupported: 'This browser cannot record audio. You can type your report.',
  micInsecure:
    'Voice notes need the secure link that starts with https://. Scan the QR code again, or type your report here.',
  micFailed: 'Recording did not start. Try again, or type your report.',
  micEmpty: 'That recording was empty. Try again and hold the phone closer.',
  typeYourReport: 'Type your report',

  errNetwork: 'Could not reach FloodLine. Your report is still here. Check your signal and try again.',
  errTimeout: 'The connection is too slow. Your report is still here. Try again.',
  errServer: 'FloodLine could not save the report. Your report is still here. Try again.',
  errTooLarge: 'The photo or recording is too large. Remove the photo and try again.',
  errMissing: 'Record a voice note or write a few words first.',

  doneTitle: 'Report received',
  reportNumber: (id) => `Report number ${id}`,
  understanding: 'Understanding your report…',
  savedGeneric: 'Your report is saved. Responders can see it on their map now.',
  understoodHeading: 'What we understood',
  chipDepth: (cm) => `Water about ${cm} cm (${inches(cm)} in)`,
  chipRising: 'Water rising',
  chipLivingSpace: 'Water inside the home',
  chipBasement: 'In a basement',
  chipCar: 'In a car',
  chipTrapped: 'Someone trapped',
  chipMedical: 'Needs medical help',
  chipElderly: 'Older person',
  chipChildren: 'Children',
  chipDisabled: 'Person with a disability',
  chipPeople: (n) => `${n} people`,
  done911: 'If life is in danger, call 911 now.',
  sendAnother: 'Send another report',

  seconds: (n) => (n === 1 ? '1 second' : `${n} seconds`),
  annRecording: 'Recording. Speak now.',
  annStopped: (d) => `Recording stopped. ${d}.`,
  annCancelled: 'Recording cancelled.',
  annSending: 'Sending your report.',
  annReceived: (id) => `Report received. Report number ${id}.`,
  annUnderstood: 'Your report was understood.',
}

function arabicPeople(n: number): string {
  if (n === 2) return 'شخصان'
  if (n >= 3 && n <= 10) return `${n} أشخاص`
  return `${n} شخصاً`
}

function arabicSeconds(n: number): string {
  if (n === 1) return 'ثانية واحدة'
  if (n === 2) return 'ثانيتان'
  if (n >= 3 && n <= 10) return `${n} ثوانٍ`
  return `${n} ثانية`
}

const ar: Strings = {
  docTitle: 'الإبلاغ عن فيضان – FloodLine',
  langShort: 'عربي',
  langFull: 'العربية',
  langSwitcher: 'اللغة',
  emergency: 'في حالة خطر على الحياة، اتصل بـ 911',

  idleTitle: 'أبلِغ عن فيضان',
  idleLead: 'اضغط الزر الأحمر واحكِ لنا ما يحدث، بأي لغة.',
  record: 'سجّل',
  recordAria: 'ابدأ تسجيل رسالة صوتية',
  recordHint: 'قل لنا أين أنت، وكم عمق الماء، ومن معك.',
  typeInstead: 'أفضّل الكتابة',

  recordingTitle: 'نستمع إليك…',
  recordingLead: 'تكلّم الآن، واضغط الزر عندما تنتهي.',
  allowMic: 'اسمح باستخدام الميكروفون إذا طلب الهاتف ذلك.',
  stop: 'إيقاف',
  stopAria: 'أوقف التسجيل',
  autoStopNote: 'يتوقف التسجيل تلقائياً بعد دقيقة واحدة.',
  cancel: 'إلغاء',

  reviewTitle: 'رسالتك الصوتية جاهزة',
  reviewLead: 'استمع إليها إن أردت، ثم أرسلها.',
  play: 'استمع',
  pause: 'إيقاف مؤقت',
  recordAgain: 'سجّل من جديد',
  addDetails: 'أضف تفاصيل مكتوبة',
  detailsLabel: 'تفاصيل مكتوبة (اختياري)',
  send: 'أرسل البلاغ',
  tryAgain: 'حاول مرة أخرى',

  addPhoto: 'أضف صورة',
  photoAdded: 'تمت إضافة الصورة',
  removePhoto: 'احذف الصورة',
  photoOptional: 'اختياري. صورة للماء تساعد فرق الإنقاذ.',

  typeTitle: 'اكتب بلاغك',
  typeLead: 'احكِ لنا ما يحدث، بأي لغة.',
  typeLabel: 'ماذا يحدث؟',
  typePlaceholder: 'مثال: الماء في القبو وصل إلى الركبة وما زال يرتفع. أمي عمرها 80 سنة ولا تقدر تطلع الدرج.',
  useVoice: 'أفضّل تسجيل رسالة صوتية',
  textEmpty: 'اكتب أولاً بضع كلمات عمّا يحدث.',

  sending: 'جارٍ إرسال بلاغك…',
  sendingLead: 'أبقِ هذه الصفحة مفتوحة.',

  locating: 'جارٍ تحديد موقعك…',
  locFound: (m) => `تم تحديد موقعك (±${m} م)`,
  locUnavailable: 'تعذّر تحديد موقعك.',
  locDenied: 'خدمة الموقع مغلقة لهذه الصفحة.',
  locInsecure: 'تحديد الموقع يحتاج إلى الرابط الآمن https.',
  locRetry: 'حاول مجدداً',
  addressLabel: 'الشارع وأقرب تقاطع',
  addressPlaceholder: 'مثال: شارع وارن عند تقاطع شيفر',
  addressHint: 'أو اذكر المكان في رسالتك الصوتية.',

  micDenied: 'الميكروفون محظور. اسمح به من إعدادات المتصفح، أو اكتب بلاغك.',
  micNotFound: 'لا يوجد ميكروفون في هذا الهاتف. يمكنك كتابة بلاغك.',
  micBusy: 'تطبيق آخر يستخدم الميكروفون. أغلقه وحاول مجدداً، أو اكتب بلاغك.',
  micUnsupported: 'هذا المتصفح لا يستطيع تسجيل الصوت. يمكنك كتابة بلاغك.',
  micInsecure: 'الرسائل الصوتية تحتاج إلى الرابط الآمن الذي يبدأ بـ https://. امسح رمز QR مرة أخرى، أو اكتب بلاغك هنا.',
  micFailed: 'لم يبدأ التسجيل. حاول مجدداً، أو اكتب بلاغك.',
  micEmpty: 'التسجيل فارغ. حاول مجدداً وقرّب الهاتف منك.',
  typeYourReport: 'اكتب بلاغك',

  errNetwork: 'تعذّر الوصول إلى FloodLine. بلاغك ما زال هنا. تأكد من الإشارة وحاول مرة أخرى.',
  errTimeout: 'الاتصال بطيء جداً. بلاغك ما زال هنا. حاول مرة أخرى.',
  errServer: 'لم يتمكن FloodLine من حفظ البلاغ. بلاغك ما زال هنا. حاول مرة أخرى.',
  errTooLarge: 'الصورة أو التسجيل كبير جداً. احذف الصورة وحاول مرة أخرى.',
  errMissing: 'سجّل رسالة صوتية أو اكتب بضع كلمات أولاً.',

  doneTitle: 'وصلنا بلاغك',
  reportNumber: (id) => `رقم البلاغ ${id}`,
  understanding: 'نقرأ بلاغك الآن…',
  savedGeneric: 'بلاغك محفوظ، وفرق الاستجابة تراه الآن على خريطتها.',
  understoodHeading: 'ما فهمناه',
  chipDepth: (cm) => `عمق الماء نحو ${cm} سم (${inches(cm)} إنش)`,
  chipRising: 'الماء يرتفع',
  chipLivingSpace: 'الماء داخل البيت',
  chipBasement: 'في القبو',
  chipCar: 'في سيارة',
  chipTrapped: 'شخص عالق',
  chipMedical: 'يحتاج مساعدة طبية',
  chipElderly: 'شخص كبير في السن',
  chipChildren: 'أطفال',
  chipDisabled: 'شخص من ذوي الإعاقة',
  chipPeople: arabicPeople,
  done911: 'إذا كانت هناك حياة في خطر، اتصل بـ 911 الآن.',
  sendAnother: 'أرسل بلاغاً آخر',

  seconds: arabicSeconds,
  annRecording: 'التسجيل يعمل. تكلّم الآن.',
  annStopped: (d) => `توقف التسجيل. ${d}.`,
  annCancelled: 'أُلغي التسجيل.',
  annSending: 'جارٍ إرسال بلاغك.',
  annReceived: (id) => `وصلنا بلاغك. رقم البلاغ ${id}.`,
  annUnderstood: 'فهمنا بلاغك.',
}

const es: Strings = {
  docTitle: 'Reportar una inundación – FloodLine',
  langShort: 'ES',
  langFull: 'Español',
  langSwitcher: 'Idioma',
  emergency: 'Si hay peligro para la vida, llame al 911',

  idleTitle: 'Reporte una inundación',
  idleLead: 'Toque el botón rojo y cuéntenos qué pasa, en el idioma que quiera.',
  record: 'Grabar',
  recordAria: 'Empezar a grabar un mensaje de voz',
  recordHint: 'Diga dónde está, qué tan alta está el agua y quién está con usted.',
  typeInstead: 'Prefiero escribir',

  recordingTitle: 'Le escuchamos…',
  recordingLead: 'Hable ahora. Toque el botón cuando termine.',
  allowMic: 'Permita el micrófono si el teléfono lo pide.',
  stop: 'Detener',
  stopAria: 'Detener la grabación',
  autoStopNote: 'La grabación se detiene sola después de 1 minuto.',
  cancel: 'Cancelar',

  reviewTitle: 'Su mensaje de voz está listo',
  reviewLead: 'Escúchelo si quiere y luego envíelo.',
  play: 'Escuchar',
  pause: 'Pausa',
  recordAgain: 'Grabar de nuevo',
  addDetails: 'Agregar detalles por escrito',
  detailsLabel: 'Detalles por escrito (opcional)',
  send: 'Enviar reporte',
  tryAgain: 'Intentar de nuevo',

  addPhoto: 'Agregar una foto',
  photoAdded: 'Foto agregada',
  removePhoto: 'Quitar la foto',
  photoOptional: 'Opcional. Una foto del agua ayuda a los rescatistas.',

  typeTitle: 'Escriba su reporte',
  typeLead: 'Cuéntenos qué pasa, en el idioma que quiera.',
  typeLabel: '¿Qué está pasando?',
  typePlaceholder:
    'Ejemplo: En el sótano el agua nos llega a la rodilla y sigue subiendo. Mi mamá tiene 80 años y no puede subir la escalera.',
  useVoice: 'Mejor grabar un mensaje de voz',
  textEmpty: 'Primero escriba unas palabras sobre lo que pasa.',

  sending: 'Enviando su reporte…',
  sendingLead: 'No cierre esta página.',

  locating: 'Buscando su ubicación…',
  locFound: (m) => `Ubicación encontrada (±${m} m)`,
  locUnavailable: 'No pudimos obtener su ubicación.',
  locDenied: 'La ubicación está desactivada para esta página.',
  locInsecure: 'La ubicación necesita el enlace seguro https.',
  locRetry: 'Reintentar',
  addressLabel: 'Calle y cruce más cercano',
  addressPlaceholder: 'Ejemplo: Warren Ave y Schaefer Rd',
  addressHint: 'O diga el lugar en su mensaje de voz.',

  micDenied: 'El micrófono está bloqueado. Permítalo en la configuración del navegador o escriba su reporte.',
  micNotFound: 'Este teléfono no tiene micrófono disponible. Puede escribir su reporte.',
  micBusy: 'Otra aplicación está usando el micrófono. Ciérrela e intente de nuevo, o escriba su reporte.',
  micUnsupported: 'Este navegador no puede grabar audio. Puede escribir su reporte.',
  micInsecure:
    'Los mensajes de voz necesitan el enlace seguro que empieza con https://. Escanee el código QR otra vez o escriba su reporte aquí.',
  micFailed: 'La grabación no empezó. Intente de nuevo o escriba su reporte.',
  micEmpty: 'La grabación quedó vacía. Intente de nuevo con el teléfono más cerca.',
  typeYourReport: 'Escribir mi reporte',

  errNetwork: 'No pudimos conectar con FloodLine. Su reporte sigue aquí. Revise la señal e intente de nuevo.',
  errTimeout: 'La conexión está muy lenta. Su reporte sigue aquí. Intente de nuevo.',
  errServer: 'FloodLine no pudo guardar el reporte. Su reporte sigue aquí. Intente de nuevo.',
  errTooLarge: 'La foto o la grabación es demasiado grande. Quite la foto e intente de nuevo.',
  errMissing: 'Primero grabe un mensaje de voz o escriba unas palabras.',

  doneTitle: 'Reporte recibido',
  reportNumber: (id) => `Reporte número ${id}`,
  understanding: 'Estamos leyendo su reporte…',
  savedGeneric: 'Su reporte está guardado. Los equipos de rescate ya lo ven en su mapa.',
  understoodHeading: 'Lo que entendimos',
  chipDepth: (cm) => `Agua de unos ${cm} cm (${inches(cm)} pulg.)`,
  chipRising: 'El agua sube',
  chipLivingSpace: 'Agua dentro de la casa',
  chipBasement: 'En un sótano',
  chipCar: 'En un carro',
  chipTrapped: 'Alguien atrapado',
  chipMedical: 'Necesita ayuda médica',
  chipElderly: 'Persona mayor',
  chipChildren: 'Niños',
  chipDisabled: 'Persona con discapacidad',
  chipPeople: (n) => `${n} personas`,
  done911: 'Si hay peligro para la vida, llame al 911 ahora.',
  sendAnother: 'Enviar otro reporte',

  seconds: (n) => (n === 1 ? '1 segundo' : `${n} segundos`),
  annRecording: 'Grabando. Hable ahora.',
  annStopped: (d) => `Grabación detenida. ${d}.`,
  annCancelled: 'Grabación cancelada.',
  annSending: 'Enviando su reporte.',
  annReceived: (id) => `Reporte recibido. Número ${id}.`,
  annUnderstood: 'Entendimos su reporte.',
}

export const STRINGS: Record<UiLanguage, Strings> = { en, ar, es }
export const LANGUAGES: UiLanguage[] = ['en', 'ar', 'es']

export function isRtl(lang: UiLanguage): boolean {
  return lang === 'ar'
}

const STORAGE_KEY = 'floodline.report.lang'

function asLanguage(value: string | null | undefined): UiLanguage | null {
  const code = (value || '').toLowerCase().slice(0, 2)
  return code === 'en' || code === 'ar' || code === 'es' ? code : null
}

/** Saved choice first, then the phone's own languages in order, then English. */
export function initialLanguage(): UiLanguage {
  try {
    const saved = asLanguage(window.localStorage.getItem(STORAGE_KEY))
    if (saved) return saved
  } catch {
    // storage blocked (private mode, old Safari); fall back to the browser language
  }
  const prefs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language]
  for (const pref of prefs) {
    const lang = asLanguage(pref)
    if (lang) return lang
  }
  return 'en'
}

export function saveLanguage(lang: UiLanguage): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, lang)
  } catch {
    // not fatal: the choice just will not survive a reload
  }
}
