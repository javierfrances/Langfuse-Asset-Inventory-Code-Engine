# Langfuse Asset Inventory — IBM Cloud Code Engine (COS ONLY)

Job de inventario incremental de recursos observados en Langfuse: **Agents, Tools, Knowledge Bases, MCP Servers y Foundation Models**.

**Persistencia exclusivamente en IBM Cloud Object Storage (COS).** El Job descarga el inventario `inventario_langfuse.json` del bucket, incorpora los recursos nuevos deduplicando por identidad y vuelve a subir el JSON. Si el objeto aún no existe, el Job lo crea tras el primer escaneo. Si COS no responde o no hay credenciales válidas, el Job falla: **no hay modo local ni fallback a almacenamiento efímero**.

No se almacenan trazas completas, prompts ni CVs en el inventario; solo datos de identificación, metadatos selectivos y referencias. La ausencia de modelos, MCPs o bases de conocimiento en las trazas no implica que se inventaríen artificialmente.

## Archivos

```
inventariar_langfuse.py  # extractor vía Langfuse Observations API v2
run_job.py               # descarga, ejecuta y sube el inventario a COS
Dockerfile               # para construir desde GitHub a Code Engine
requirements.txt         # SDK de IBM COS, obligatorio
.env.example             # plantilla; no contiene credenciales reales
.gitignore
.dockerignore
tests/test_inventory.py  # pruebas unitarias sin conexión a la nube
```

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `LANGFUSE_BASE_URL` | Sí | Ej. `https://cloud.langfuse.com` |
| `LANGFUSE_PROJECT_ID` | Sí | ID del proyecto Langfuse |
| `LANGFUSE_PUBLIC_KEY` | Sí, Secret | API public key |
| `LANGFUSE_SECRET_KEY` | Sí, Secret | API secret key |
| `COS_ENDPOINT` | Sí | Endpoint regional HTTPS, según región del bucket |
| `COS_API_KEY` | Sí, Secret | API key de COS con lectura/escritura en el bucket |
| `COS_SERVICE_INSTANCE_ID` | Sí | CRN `resource_instance_id` de COS |
| `COS_BUCKET` | Sí | Bucket COS existente |
| `COS_OBJECT_KEY` | No | Por defecto `inventario_langfuse.json` |
| `INVENTORY_DAYS` | No | Por defecto `30`; ventana retrospectiva en días |
| `INVENTORY_FROM` / `INVENTORY_TO` | No | Intervalo ISO UTC opcional (`TO` exclusivo) |
| `INVENTORY_PAGE_SIZE` | No | Por defecto `500`; máximo `1000` |
| `INVENTORY_INSPECT_IO` | No | `false`; lectura selectiva de IDs de I/O estructurado |
| `INVENTORY_DRY_RUN` | No | `false`; lee COS y Langfuse pero no sube a COS |
| `INVENTORY_PRINT_JSON` | No | `false`; evita imprimir el inventario completo en logs |

No existe `INVENTORY_STORAGE` ni un modo `local`. El JSON descargado se usa únicamente en una carpeta temporal durante el JobRun, **no es la persistencia**.

## Pruebas locales (WSL)

Instala dependencias en un entorno virtual si quieres ejecutarlo en WSL (evita el error PEP 668):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

Carga las variables por tu método habitual sin subir credenciales a GitHub. Si usas un `.env` local con `KEY=value`, puedes cargarlas con `set -a; source .env; set +a`. Ejecuta:

```bash
python3 run_job.py
# Una segunda ejecución descargará el inventario del bucket y solo añadirá recursos nuevos.
```

## GitHub y Code Engine

Descomprime el ZIP, sube **los archivos** a la raíz de un repositorio GitHub (no solo el ZIP). Confirma que `Dockerfile` está en la raíz. El repositorio puede ser privado, con el acceso al repo configurado en el build.

1. Crea una instancia COS, un bucket existente en la región deseada y unas credenciales con acceso de lectura y escritura al bucket. Verifica el endpoint HTTPS correcto y el CRN de la instancia de COS.
2. Selecciona un proyecto de Code Engine:

   ```bash
   ibmcloud login --sso
   ibmcloud ce project select --name TU_PROYECTO_CODE_ENGINE
   ```

3. En **Code Engine → Secrets**, crea un Secret genérico llamado `langfuse-inventory-credentials` con estas claves: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `COS_API_KEY`. Recomiendo la consola para evitar dejar claves en el historial del terminal. Opcionalmente, también puedes guardar `COS_SERVICE_INSTANCE_ID` en el Secret.
4. Crea el Job directamente desde el código de GitHub:

   ```bash
   ibmcloud ce job create \
     --name langfuse-inventory \
     --build-source https://github.com/TU_USUARIO/langfuse-asset-inventory \
     --env-from-secret langfuse-inventory-credentials \
     --env LANGFUSE_BASE_URL=https://cloud.langfuse.com \
     --env LANGFUSE_PROJECT_ID=TU_PROJECT_ID \
     --env COS_ENDPOINT=https://TU_ENDPOINT_COS \
     --env COS_SERVICE_INSTANCE_ID=TU_COS_RESOURCE_INSTANCE_CRN \
     --env COS_BUCKET=TU_BUCKET \
     --env COS_OBJECT_KEY=inventario_langfuse.json \
     --env INVENTORY_DAYS=30 \
     --wait
   ```

5. Lanza una primera ejecución y consulta los logs:

   ```bash
   ibmcloud ce jobrun submit --job langfuse-inventory --name langfuse-inventory-test
   ibmcloud ce jobrun logs --jobrun langfuse-inventory-test
   ```

Code Engine construirá la imagen desde el `Dockerfile` con `ibm-cos-sdk` incluido. El JobRun creará o actualizará el objeto `inventario_langfuse.json` en COS. La primera ejecución puede encontrar el objeto inexistente: esto es normal. **El bucket debe existir previamente**.

## Cron diario (opcional)

```bash
ibmcloud ce sub cron create \
  --name langfuse-inventory-daily \
  --destination-type job \
  --destination langfuse-inventory \
  --schedule '0 8 * * *' \
  --time-zone Europe/Madrid
```

Ejecuta **un JobRun escritor cada vez**. COS es un almacén de objetos, no una base de datos transaccional: dos JobRuns simultáneos podrían sobrescribir el inventario uno del otro. Se recomienda habilitar versionado del bucket o hacer copias de seguridad. Las lecturas/escrituras y los fallos de Langfuse abortan el Job sin sustituir un inventario previo salvo cuando se ha completado el proceso correctamente.

## Validaciones y seguridad

- Se valida la presencia de **todas** las variables necesarias antes de comenzar. No es posible cambiar el destino a almacenamiento local.
- Fallan los errores de COS distintos de la ausencia del objeto; no se crea un inventario nuevo si el bucket o las credenciales dan error.
- Se valida el JSON descargado; un archivo corrupto o perteneciente a otro proyecto no se reemplaza.
- La API v2 de Langfuse se consulta en modo lectura con paginación.
- Las credenciales no se incorporan ni a la imagen ni al fichero JSON; no las subas a GitHub.
- `INVENTORY_DRY_RUN=true` descarga COS y calcula, pero **no actualiza COS**.

## Documentación oficial

- [IBM COS Python SDK](https://cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-python)
- [Code Engine Jobs desde GitHub](https://cloud.ibm.com/docs/codeengine?topic=codeengine-run-job-source-code)
- [Code Engine Secrets](https://cloud.ibm.com/docs/codeengine?topic=codeengine-secret)
- [Periodic Timer / cron](https://cloud.ibm.com/docs/codeengine?topic=codeengine-subscribe-cron)
- [Langfuse Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)
