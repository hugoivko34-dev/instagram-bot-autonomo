# Pedido de aprobación a Meta (App Review) — paso a paso

Objetivo: que el bot pueda leer y responder mensajes de **cualquier persona**, no solo de evaluadores.
Permiso a pedir: **instagram_business_manage_messages** con **Advanced Access**.
Todo se hace en **developers.facebook.com → tu app**, con tu sesión iniciada.

---

## 1. Antes de pedir la revisión (configuración básica)

En **Configuración de la app → Básica**:

| Campo | Qué poner |
|---|---|
| URL de la política de privacidad | `https://github.com/hugoivko34-dev/instagram-bot-autonomo/blob/main/docs/privacidad.md` |
| URL de instrucciones para eliminar datos | la misma URL (tiene la sección "Cómo pedir que borremos tus datos") |
| Categoría | Negocios y páginas (Business and Pages) |
| Ícono de la app | 1024×1024 px — podés usar el logo de Ivko Maderas |
| Correo de contacto | tu mail |

Guardá los cambios.

## 2. Agregar tu cuenta personal como evaluador (para grabar el video)

1. **Roles de la app → Roles → Agregar personas → Evaluador de Instagram** → tu usuario personal.
2. En el Instagram personal: **Configuración → Sitios web y apps → Invitaciones de evaluador → Aceptar**.
3. Mandá un mensaje a @ivkomaderas_ y confirmá que el bot responde (avisame y lo reviso).

## 3. Grabar el video (screencast) — 1 a 2 minutos

Meta quiere ver **de punta a punta** cómo se usa el permiso. Grabá la pantalla de la PC
(Windows: tecla **Win + Alt + R** inicia/detiene la grabación con la Xbox Game Bar) mostrando:

1. **Inicio de sesión**: abrí el panel del bot (Abrir_Panel.bat) y mostrá que está conectado a @ivkomaderas_
   (pestaña Resumen / Ajustes → "Probar Instagram").
2. **Mensaje entrante**: desde el celular (cuenta evaluadora) mandá *"Hola, cuánto sale pulir 30 m2 en satinado?"*.
   Mostrá en la PC (pestaña Mensajes del panel) que el mensaje llega.
3. **Respuesta automática**: mostrá en Instagram (instagram.com en la PC, cuenta evaluadora) la respuesta del bot
   con el presupuesto.
4. **Derivación**: mandá *"Prefiero que vengan a ver el piso"* y mostrá la respuesta con el WhatsApp y la alerta
   que llega a Telegram.
5. **Borrado de datos**: mandá *"borrar mis datos"* y mostrá la confirmación.

Consejos: subtítulos o texto en inglés en pantalla ayudan (podés agregarlos con el editor de video de Windows /
Clipchamp); que se lea bien; sin música.

## 4. Pedir la revisión

**Revisión de la app → Permisos y funciones** → buscá **instagram_business_manage_messages** →
**Solicitar acceso avanzado**. Si también lo pide, solicitá **instagram_business_basic**.

### Texto para "¿Cómo usará tu app este permiso?" (copiar y pegar, en inglés)

> Ivko Maderas is a small wood-floor finishing business in Buenos Aires, Argentina (Instagram: @ivkomaderas_).
> This app is used only by the business to manage its own Instagram professional account's direct messages.
>
> We use instagram_business_manage_messages to:
> 1. Read the direct messages that potential customers send to @ivkomaderas_ asking about our services
>    (floor sanding, varnishing, decks, stairs).
> 2. Reply automatically within the 24-hour messaging window with information about our services, answers to
>    frequently asked questions, and an approximate price based on the square meters the customer provides.
> 3. When the customer asks for an on-site visit or for a service that requires a human (installations, stairs,
>    repairs), the app replies with the business WhatsApp link and notifies the owner privately so they can
>    follow up personally.
>
> Messages are processed on the business's own computer and are never sold or shared with third parties. Users
> can request deletion of their data by sending "borrar mis datos" / "delete my data", which the app handles
> automatically. Without this permission, customers who message us outside business hours would not receive any
> answer.

### Texto para "Instrucciones paso a paso para el revisor" (en inglés)

> 1. The app runs as a backend service for the Instagram professional account @ivkomaderas_; there is no public
>    login screen for end users.
> 2. Send a direct message to @ivkomaderas_ from any Instagram account, for example:
>    "Hola, cuánto sale pulir 30 m2 en satinado?" (How much is sanding 30 m2 with satin finish?).
> 3. Within about one minute the app replies with an approximate quote in Spanish.
> 4. Send "Prefiero que vengan a ver el piso" (I prefer an on-site visit): the app replies with the business
>    WhatsApp link and alerts the owner.
> 5. Send "delete my data": the app deletes all stored data for that user and confirms.
> The attached screencast shows this full flow.

## 5. Después de la aprobación

1. Arriba en el panel de la app, pasá el interruptor de **Desarrollo** a **Activo (Live)**.
2. Mandá un mensaje desde una cuenta que **no** sea evaluadora y confirmá que el bot responde.

Puede pedirte también la **verificación del negocio** (Business Manager → Configuración → Centro de seguridad):
datos del negocio y un comprobante (factura de servicio, constancia de AFIP/ARCA, etc.).

---

**Nota:** Instagram solo permite responder dentro de las **24 horas** desde el último mensaje del cliente; el bot
revisa los mensajes cada pocos minutos, así que no es un problema.
