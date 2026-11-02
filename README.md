# Consulta archivos CSV con preguntas

BUSRAG es una aplicación que permite hacer preguntas sobre la información de un archivo CSV. Por defecto, este proyecto está configurado para usar `resources/data/uber_drives.csv`, con registros de viajes de Uber de 2016.

Escribes una pregunta en español y la aplicación busca filas relacionadas en el archivo. Después, un modelo de lenguaje redacta una respuesta usando esas filas como referencia.

## Antes de empezar

Necesitas instalar estas herramientas en tu computadora:

- **Python 3.11** y **Poetry**, para instalar las dependencias del proyecto.
- **Podman** con su máquina virtual iniciada, para ejecutar OpenSearch, LocalStack y la aplicación en contenedores. También puedes usar Docker con los comandos equivalentes.
- **Ollama**, para ejecutar localmente los modelos de chat y embeddings.

En macOS, inicia Podman una vez antes de continuar:

```bash
podman machine start
```

Si Podman indica que no existe una máquina, créala y vuelve a iniciarla:

```bash
podman machine init
podman machine start
```

## Preparar el proyecto

Abre una terminal en la carpeta del proyecto. Si todavía no tienes un archivo `.env`, créalo copiando el ejemplo:

```bash
cp .env.example .env
```

`.env` contiene la configuración local, como la fuente CSV y el índice de OpenSearch. El ejemplo ya apunta a Uber:

```dotenv
SOURCE_CSV_PATH=resources/data/uber_drives.csv
OPENSEARCH_INDEX=uber_drives
```

Instala las dependencias de Python:

```bash
make install
```

## Iniciar servicios y cargar los viajes

Ejecuta:

```bash
make ingest
```

Este comando prepara OpenSearch, Ollama y LocalStack si todavía no están activos. También descarga los modelos configurados, guarda el CSV original en el almacenamiento local de S3 y crea una representación de búsqueda para cada fila del archivo. La primera ejecución puede tardar mientras se descargan los modelos y se procesan los viajes.

Para que la API pueda recibir y procesar archivos desde Swagger, también prepara su función de ingesta local:

```bash
make setup-lambda
```

Si `make ingest` termina correctamente, verás un mensaje parecido a:

```text
Ingesta completada: 1155 documentos indexados, 0 errores.
```

## Construir y ejecutar la aplicación con Podman

Construye la imagen desde la carpeta raíz del proyecto:

```bash
podman build -t busrag-api:local .
```

Inicia la API:

```bash
podman run --rm --name busrag-api --env-file .env \
  -e OPENSEARCH_HOST=host.containers.internal \
  -e S3_ENDPOINT_URL=http://host.containers.internal:4566 \
  -e OPENAI_ENDPOINT=http://host.containers.internal:11434/v1 \
  -e EMBEDDING_ENDPOINT=http://host.containers.internal:11434/v1 \
  -p 8000:8000 busrag-api:local
```

La terminal mostrará que Uvicorn está escuchando en el puerto 8000. Déjala abierta mientras usas la aplicación.

Abre estas direcciones en el navegador:

- Aplicación web: <http://localhost:8000>
- Swagger, para probar los endpoints: <http://localhost:8000/docs>

Para detener la aplicación, vuelve a la terminal donde corre y pulsa **Ctrl+C**. Como el comando incluye `--rm`, Podman elimina el contenedor detenido; la imagen y los datos de OpenSearch se conservan.

### Si usas Docker Desktop

En el comando anterior, cambia `host.containers.internal` por `host.docker.internal` en las cuatro variables `-e`. El resto del comando no cambia.

## Comprobar que todo funciona

En otra terminal ejecuta:

```bash
curl http://localhost:8000/health
```

Cuando todos los servicios estén disponibles, la respuesta será parecida a esta:

```json
{"status":"ok","s3":"ok","lambda":"ok","opensearch":"ok"}
```

Swagger también permite comprobar cada ruta. Abre una ruta, pulsa **Try it out**, completa los datos y pulsa **Execute**.

| Método | Ruta | Para qué sirve |
| --- | --- | --- |
| `GET` | `/health` | Comprueba la API y sus servicios |
| `POST` | `/ask` | Hace una pregunta sobre el CSV ya cargado |
| `POST` | `/documents/upload` | Sube otro archivo CSV |
| `POST` | `/documents/ingest` | Procesa e indexa el archivo que se subió |

Ejemplo del cuerpo JSON para `POST /ask`:

```json
{
  "message": "Muéstrame viajes con propósito Meeting"
}
```

La respuesta contiene el texto generado y los registros que se usaron como fuentes.

### Cargar otro archivo desde Swagger

1. En Swagger, abre `POST /documents/upload` y pulsa **Try it out**.
2. Selecciona un CSV UTF-8 de hasta 20 MiB y pulsa **Execute**.
3. Copia el valor `document_id` de la respuesta.
4. Abre `POST /documents/ingest`, pulsa **Try it out** y pega ese identificador en el cuerpo JSON:

   ```json
   {
     "document_id": "PEGA-AQUÍ-EL-ID-DEVUELTO"
   }
   ```

5. Cuando termine la ingesta, pregunta sobre ese archivo con `POST /ask`.

## Cambiar la fuente CSV predeterminada

Coloca el CSV dentro de `resources/data/` y edita `.env`. Por ejemplo, si el archivo se llama `ventas.csv`:

```dotenv
SOURCE_CSV_PATH=resources/data/ventas.csv
OPENSEARCH_INDEX=ventas
```

Usa un nombre de índice distinto para cada fuente para que las búsquedas no mezclen registros de archivos diferentes. Después vuelve a cargar la fuente:

```bash
make ingest
```

Si modificaste el código o quieres que la imagen incluya el nuevo archivo, reconstruye la imagen y reemplaza el contenedor que está corriendo.

> **Nota:** BUSRAG busca filas relevantes; no es una herramienta de cálculo sobre todo el CSV. Por ejemplo, una pregunta por el total o promedio de todas las millas puede requerir agregar una función de análisis tabular.

## ¿Qué hace cada componente?

- **La API FastAPI** recibe las preguntas y entrega respuestas. También sirve la página web y la documentación Swagger.
- **OpenSearch** guarda y busca las filas del CSV.
- **Ollama** genera embeddings para la búsqueda y respuestas en lenguaje natural.
- **LocalStack** simula S3 y Lambda en tu computadora. S3 conserva los archivos y Lambda procesa los CSV que se suben desde la API.
- **Dockerfile** describe cómo construir la imagen de la API.

Los servicios auxiliares se ejecutan por separado; construir la imagen de la API no los instala.

## Solución de problemas

### `podman machine start` no puede conectarse

Inicia la máquina de Podman. En macOS, ejecuta `podman machine start` y espera a que termine antes de construir o arrancar contenedores.

### El puerto 8000 ya está ocupado o el nombre `busrag-api` ya existe

Puede haber otra instancia ejecutándose. Deténla con:

```bash
podman stop busrag-api
```

El contenedor se elimina automáticamente porque se inició con `--rm`. Luego vuelve a ejecutar `podman run`.

### `/health` muestra `"lambda":"error"`

Prepara o actualiza la función local con:

```bash
make setup-lambda
```

### `make setup-localstack` no encuentra `host.containers.internal`

Los comandos `make` se ejecutan en tu Mac, por eso `.env` debe usar `localhost` para OpenSearch, S3 y Ollama. Los valores `host.containers.internal` se pasan solo en el comando `podman run`, para que el contenedor encuentre los servicios del Mac.

### `/ask` no encuentra viajes

Asegúrate de que `SOURCE_CSV_PATH` señale el archivo correcto y que `OPENSEARCH_INDEX` sea el mismo índice que usa la API. Después ejecuta `make ingest` y vuelve a preguntar.

## Despliegue en la nube: estrategia general

La estrategia prevista es desplegar la API como contenedor en un servicio de aplicaciones o contenedores en la nube. En términos generales:

1. Construir la imagen con el `Dockerfile`.
2. Publicarla en un registro de contenedores accesible para la nube.
3. Crear una aplicación o servicio que ejecute esa imagen y exponga el puerto `8000`.
4. Configurar variables de entorno y secretos en la plataforma.
5. Conectar la API a servicios de almacenamiento, búsqueda y modelos accesibles desde la nube.
6. Revisar el estado de la aplicación y probar `/health` y `/docs`.

LocalStack, las credenciales de ejemplo y las direcciones `localhost` son para desarrollo local; no deben usarse como servicios de producción. La plataforma elegida deberá proporcionar o conectar servicios de nube equivalentes.
