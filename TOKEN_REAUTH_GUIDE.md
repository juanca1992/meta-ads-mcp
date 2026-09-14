# Reautorización del token de Meta Ads

Guía completa para configurar la app de Meta, autorizar los permisos necesarios y cargar el token en el MCP sin exponerlo.

## 1. Requisitos

- Tener acceso de administrador, desarrollador o tester de la app de Meta.
- Tener acceso a las cuentas publicitarias y páginas que se utilizarán.
- Tener configurados en `.env` `META_APP_ID` y `META_APP_SECRET`.
- Ejecutar los comandos desde la raíz del repositorio.

Nunca publiques `.env`, `token_cache.json`, `META_APP_SECRET` ni el valor de `META_ACCESS_TOKEN`.

## 2. Configurar el caso de uso

1. Entra en [Meta for Developers](https://developers.facebook.com/apps/).
2. Abre la aplicación correspondiente a `META_APP_ID`.
3. En el panel, pulsa **Agregar casos de uso**.
4. Selecciona:

   **Captar y administrar clientes potenciales de anuncios con la API de marketing**.

Este es el caso de uso para formularios instantáneos y recuperación de leads.

## 3. Confirmar permisos

Dentro de **Permisos y funciones**, confirma que estén disponibles para prueba:

```text
ads_management
business_management
leads_retrieval
pages_manage_ads
pages_manage_metadata
pages_read_engagement
pages_show_list
```

El estado esperado para un usuario que es rol de la app es **Listo para la prueba**. No es necesario enviar una revisión de Meta para probar con usuarios administradores, desarrolladores o testers.

## 4. Configurar Facebook Login

En **Inicio de sesión con Facebook → Configuración**:

- Mantén activo **Inicio de sesión del cliente de OAuth**.
- Mantén activo **Inicio de sesión de OAuth web**.
- Mantén activo **Usar modo estricto para URI de redireccionamiento**.

El MCP utiliza este callback local:

```text
http://localhost:8080/callback
```

### Si aparece un error de dominio

Meta permite `localhost` automáticamente cuando la app está en modo desarrollo, pero no acepta `localhost` en **Dominios de la app** cuando la app está publicada.

Si aparece:

> El dominio de esta URL no está incluido en los dominios de la app.

haz lo siguiente:

1. Elimina `localhost` de **Dominios de la app**; no agregues `localhost.com`.
2. Ve a **Publicar**.
3. Cambia la app a **En desarrollo** y confirma.
4. Deja vacío **Dominios de la app**.
5. Guarda los cambios.

En modo desarrollo, el usuario que autoriza debe ser un rol de la app.

Una aplicación que deba permanecer publicada necesita un callback HTTPS real en un dominio propio y una adaptación del servidor; no debe sustituirse por un dominio falso.

## 5. Evitar que se use el token anterior

Antes de iniciar OAuth, abre `.env` y elimina el valor antiguo o deja la variable vacía:

```dotenv
META_ACCESS_TOKEN=
```

No pegues el token en el chat ni lo incluyas en comandos que puedan quedar registrados.

## 6. Ejecutar la reautorización

Desde la raíz del proyecto:

```bash
set -a
source .env
set +a
unset META_ACCESS_TOKEN
.venv/bin/python -m meta_ads_mcp --login
```

El MCP inicia un callback local, abre el navegador y muestra una URL de Meta. Inicia sesión con el usuario autorizado y acepta los permisos solicitados.

La señal de éxito es:

```text
Authentication successful!
Authenticated as: <nombre> (ID: <id>)
```

El token se guarda en la caché local; su valor nunca debe imprimirse.

Nota para formularios v26: Meta puede exigir `FollowUpActionURL`; el MCP lo
envía mediante `follow_up_action_url` (por ejemplo, `https://mionko.com/`).
Para preguntas telefónicas, el tipo vigente es `PHONE`, no `PHONE_NUMBER`.

## 7. Ubicación y permisos de la caché

En Linux, el archivo es:

```text
~/.config/meta-ads-mcp/token_cache.json
```

El directorio debe ser privado (`0700`) y el archivo debe ser privado (`0600`). No subas este archivo al repositorio.

## 8. Cargar el token como variable de entorno

Para cargar el token de la caché en la sesión actual sin mostrarlo:

```bash
export META_ACCESS_TOKEN="$(
  .venv/bin/python -c 'import json, pathlib; print(json.loads((pathlib.Path.home()/".config/meta-ads-mcp/token_cache.json").read_text())["access_token"])'
)"
```

Verifica solo su presencia:

```bash
if [ -n "$META_ACCESS_TOKEN" ]; then
  echo "META_ACCESS_TOKEN cargado correctamente"
else
  echo "No se pudo cargar el token"
fi
```

No ejecutes `echo "$META_ACCESS_TOKEN"`.

Si se desea cargarlo al abrir una terminal, puede crearse un alias o función local que ejecute el mismo `export`. No se recomienda guardar el valor literal del token en `.zshrc` o `.bashrc`.

## 9. Verificación de permisos

La comprobación de solo lectura debe mostrar:

```text
ads_management=granted
business_management=granted
leads_retrieval=granted
pages_manage_ads=granted
pages_manage_metadata=granted
pages_read_engagement=granted
pages_show_list=granted
```

Si alguno aparece como `not_listed`, `declined` o `expired`, repite la autorización después de corregir el caso de uso o el modo de la app.

## 10. Siguiente paso

Cuando todos los permisos estén concedidos:

1. Elegir la cuenta publicitaria.
2. Elegir la página de Facebook.
3. Validar la configuración con `validate_only`.
4. Crear una campaña pausada solo después de confirmar explícitamente los parámetros.

La validación no debe crear campañas, conjuntos de anuncios, anuncios ni formularios.
