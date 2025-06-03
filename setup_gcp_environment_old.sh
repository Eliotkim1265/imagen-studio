#!/bin/bash

# Script to set up the Media Studio Django App environment on Google Cloud Platform
# IMPORTANT: This script makes changes to your GCP project. Review carefully.

set -e # Exit immediately if a command exits with a non-zero status.
# set -o pipefail # Causes a pipeline to return the exit status of the last command in the pipe that failed.
# set -u # Treat unset variables as an error when substituting.

echo "----------------------------------------------------------------------"
echo " Welcome to the Media Studio GCP Environment Setup Script!"
echo "----------------------------------------------------------------------"
echo "This script will help you configure the necessary GCP services."
echo "Please ensure you have the 'gcloud' CLI installed and authenticated"
echo "with an account that has 'Owner' or sufficient administrative"
echo "permissions on the GCP project you intend to use."
echo ""
read -p "Do you wish to continue? (yes/no): " CONFIRM_CONTINUE
if [ "$CONFIRM_CONTINUE" != "yes" ]; then
  echo "Setup aborted by user."
  exit 1
fi

# --- 1. Gather Information ---
echo ""
echo "--- Section 1: Project Configuration ---"

CURRENT_PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$CURRENT_PROJECT_ID" ]; then
    read -p "Enter your Google Cloud Project ID: " GCP_PROJECT_ID
else
    read -p "Enter your Google Cloud Project ID (defaults to '$CURRENT_PROJECT_ID'): " GCP_PROJECT_ID
    GCP_PROJECT_ID=${GCP_PROJECT_ID:-$CURRENT_PROJECT_ID}
fi

if [ -z "$GCP_PROJECT_ID" ]; then
    echo "ERROR: GCP Project ID is required."
    exit 1
fi
gcloud config set project "$GCP_PROJECT_ID"
echo "Set active project to: $GCP_PROJECT_ID"

CURRENT_REGION=$(gcloud config get-value run/region 2>/dev/null)
DEFAULT_REGION="us-central1" # Default to your app's primary region
if [ -z "$CURRENT_REGION" ]; then
    read -p "Enter the GCP region for services (e.g., us-central1, europe-west1) [default: $DEFAULT_REGION]: " GCP_REGION
else
    read -p "Enter the GCP region for services (e.g., us-central1, europe-west1) [defaults to '$CURRENT_REGION']: " GCP_REGION
    GCP_REGION=${GCP_REGION:-$CURRENT_REGION}
fi
GCP_REGION=${GCP_REGION:-$DEFAULT_REGION}
gcloud config set run/region "$GCP_REGION"
echo "Set default Cloud Run region to: $GCP_REGION"


# --- 2. Enable Necessary APIs ---
echo ""
echo "--- Section 2: Enabling Google Cloud APIs ---"
echo "The following APIs will be enabled: Cloud Run, Vertex AI, Cloud Storage,"
echo "Artifact Registry (for Docker images), IAM, Cloud SQL Admin API (if creating DB),"
echo "and Cloud Build (for building containers)."
read -p "Proceed with enabling these APIs? (yes/no): " CONFIRM_APIS
if [ "$CONFIRM_APIS" == "yes" ]; then
  gcloud services enable \
    run.googleapis.com \
    aiplatform.googleapis.com \
    storage.googleapis.com \
    artifactregistry.googleapis.com \
    iam.googleapis.com \
    sqladmin.googleapis.com \
    cloudbuild.googleapis.com \
    --project="$GCP_PROJECT_ID"
  echo "APIs enabled successfully."
else
  echo "API enablement skipped. Some services might not function correctly."
fi

# --- 3. Service Account for Cloud Run ---
echo ""
echo "--- Section 3: Creating Service Account for Cloud Run ---"
SERVICE_ACCOUNT_NAME="media-studio-run-sa" # You can make this configurable
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$SERVICE_ACCOUNT_EMAIL" --project="$GCP_PROJECT_ID" &>/dev/null; then
  echo "Service account $SERVICE_ACCOUNT_EMAIL already exists."
else
  read -p "Create service account '$SERVICE_ACCOUNT_NAME'? (yes/no): " CONFIRM_SA
  if [ "$CONFIRM_SA" == "yes" ]; then
    gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
      --display-name="Media Studio Cloud Run Service Account" \
      --project="$GCP_PROJECT_ID"
    echo "Service account $SERVICE_ACCOUNT_EMAIL created."
  else
    echo "Service account creation skipped. You'll need to ensure an appropriate service account exists."
    # Potentially ask for an existing SA email here
  fi
fi

# --- 4. GCS Bucket Setup ---
echo ""
echo "--- Section 4: Google Cloud Storage Bucket Setup ---"
DEFAULT_BUCKET_NAME="${GCP_PROJECT_ID}-media-studio-bucket" # Suggest a unique name
read -p "Enter a globally unique GCS bucket name (or press Enter for default: '$DEFAULT_BUCKET_NAME'): " GCS_BUCKET_NAME
GCS_BUCKET_NAME=${GCS_BUCKET_NAME:-$DEFAULT_BUCKET_NAME}

if gsutil ls "gs://${GCS_BUCKET_NAME}" &>/dev/null; then
  echo "GCS Bucket gs://${GCS_BUCKET_NAME} already exists."
else
  read -p "Create GCS bucket 'gs://${GCS_BUCKET_NAME}' in region '$GCP_REGION' with Uniform Bucket-Level Access? (yes/no): " CONFIRM_BUCKET
  if [ "$CONFIRM_BUCKET" == "yes" ]; then
    gsutil mb -p "$GCP_PROJECT_ID" -l "$GCP_REGION" -b on "gs://${GCS_BUCKET_NAME}"
    echo "GCS Bucket gs://${GCS_BUCKET_NAME} created."
    # Optional: Set public access for generated media if that's the chosen strategy.
    # If using signed URLs, this isn't needed. Current app assumes public URLs for objects generated by the app.
    # If the organization's users have direct IAM access, they manage their user permissions separately.
    # The app's service account needs access regardless.
    echo "Granting the Cloud Run service account ($SERVICE_ACCOUNT_EMAIL) permissions on the bucket..."
    gsutil iam ch \
      "serviceAccount:${SERVICE_ACCOUNT_EMAIL}:objectAdmin" \
      "gs://${GCS_BUCKET_NAME}"
    echo "Service account granted Storage Object Admin on the bucket."
    echo "IMPORTANT: If generated media should be public, configure 'allUsers' with 'Storage Object Viewer' on 'gs://${GCS_BUCKET_NAME}/${GCS_OBJECT_PATH_PREFIX_ENV_VAR:-media_studio_uploads/}' via IAM Console or gcloud."
  else
    echo "GCS bucket creation skipped. Ensure a bucket is configured and accessible."
  fi
fi
# Variable for the .env file, ensure it matches settings.py expectation
GCS_OBJECT_PATH_PREFIX_ENV_VAR="media_studio_uploads/"

# --- Optional: Grant Google Group Access to GCS Bucket ---
echo ""
echo "--- Section X: Grant Google Group Access to GCS Bucket (Optional) ---"
read -p "Do you want to grant GCS bucket access to a specific Google Group? (yes/no): " CONFIRM_GROUP_ACCESS

if [ "$CONFIRM_GROUP_ACCESS" == "yes" ]; then
  if [ -z "$GCS_BUCKET_NAME" ] || [ "$GCS_BUCKET_NAME" == "${GCP_PROJECT_ID}-media-studio-bucket" ] && ! gsutil ls "gs://${GCS_BUCKET_NAME}" &>/dev/null; then
    echo "WARNING: GCS Bucket '$GCS_BUCKET_NAME' does not seem to be defined or created yet in this script run. Please ensure it exists."
    # Attempt to get it again if it was set earlier by default but not confirmed for creation
    DEFAULT_BUCKET_NAME_FOR_GROUP="${GCP_PROJECT_ID}-media-studio-bucket" # Assuming this was the default
    read -p "Enter the GCS bucket name to grant group access to (e.g., ${DEFAULT_BUCKET_NAME_FOR_GROUP}): " TARGET_GCS_BUCKET_FOR_GROUP
    if [ -z "$TARGET_GCS_BUCKET_FOR_GROUP" ]; then
        echo "Bucket name not provided. Skipping group access grant."
    else
        GCS_BUCKET_NAME="$TARGET_GCS_BUCKET_FOR_GROUP" # Use the newly provided name
    fi
  fi

  if [ -n "$GCS_BUCKET_NAME" ] && gsutil ls "gs://${GCS_BUCKET_NAME}" &>/dev/null; then
    read -p "Enter the email address of the Google Group (e.g., my-team@googlegroups.com or my-team@yourdomain.com): " GROUP_EMAIL
    if [ -z "$GROUP_EMAIL" ]; then
      echo "No Google Group email provided. Skipping."
    else
      echo "Common roles: roles/storage.objectViewer (read-only), roles/storage.objectAdmin (full control of objects)."
      read -p "Enter the IAM role to grant to the group (e.g., roles/storage.objectViewer): " GROUP_ROLE
      if [ -z "$GROUP_ROLE" ]; then
        echo "No IAM role provided. Skipping."
      else
        echo "Granting '${GROUP_ROLE}' to group '${GROUP_EMAIL}' on bucket 'gs://${GCS_BUCKET_NAME}'..."
        if gsutil iam ch "group:${GROUP_EMAIL}:${GROUP_ROLE}" "gs://${GCS_BUCKET_NAME}"; then
          echo "Successfully granted '${GROUP_ROLE}' to '${GROUP_EMAIL}' on 'gs://${GCS_BUCKET_NAME}'."
        else
          echo "ERROR: Failed to grant IAM role to the group. Please check the group email, role, and your permissions."
        fi
      fi
    fi
  elif [ -n "$GCS_BUCKET_NAME" ]; then
     echo "Cannot grant group access because bucket 'gs://${GCS_BUCKET_NAME}' does not exist or is not accessible."
  fi
fi
echo "Group access configuration finished."

# --- 5. Cloud SQL (PostgreSQL) Database Setup (Simplified) ---
echo ""
echo "--- Section 5: Cloud SQL (PostgreSQL) Database Setup ---"
echo "This app requires a PostgreSQL database. You can use an existing instance or create a new one."
DB_INSTANCE_NAME="media-studio-db-pg" # Suggest a name
DB_NAME="mediastudio"
DB_USER="mediastudio_user"

read -p "Do you want to attempt to set up a Cloud SQL PostgreSQL instance? (yes/no - 'no' means you'll configure DB manually): " CONFIRM_DB_SETUP
if [ "$CONFIRM_DB_SETUP" == "yes" ]; then
  if gcloud sql instances describe "$DB_INSTANCE_NAME" --project="$GCP_PROJECT_ID" &>/dev/null; then
    echo "Cloud SQL instance '$DB_INSTANCE_NAME' seems to exist. Will attempt to create user and DB if not present."
    # Add logic to create user/db if needed, or instruct user to do so.
    # For now, assume it exists and we just need the connection name.
  else
    echo "Creating a new Cloud SQL PostgreSQL instance '$DB_INSTANCE_NAME' (this may take several minutes)..."
    echo "A default password will be generated for the 'postgres' user. You should change it."
    echo "For production, choose appropriate machine type and settings."
    # For simplicity, using a small default tier. User should customize for production.
    gcloud sql instances create "$DB_INSTANCE_NAME" \
      --database-version=POSTGRES_15 \
      --tier=db-f1-micro \
      --region="$GCP_REGION" \
      --project="$GCP_PROJECT_ID"
    echo "Cloud SQL instance '$DB_INSTANCE_NAME' created."
  fi

  echo "Creating database '$DB_NAME' if it doesn't exist..."
  gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE_NAME" --project="$GCP_PROJECT_ID" || echo "Database '$DB_NAME' might already exist."

  echo "Generating a password for database user '$DB_USER'..."
  # Generate a random password
  DB_PASSWORD=$(openssl rand -base64 16)
  echo "Creating database user '$DB_USER'..."
  gcloud sql users create "$DB_USER" \
    --instance="$DB_INSTANCE_NAME" \
    --password="$DB_PASSWORD" \
    --project="$GCP_PROJECT_ID" || echo "User '$DB_USER' might already exist. Ensure it has access to '$DB_NAME'."

  echo ""
  echo "IMPORTANT DB DETAILS (save these securely for your .env file):"
  echo "DB_USER: $DB_USER"
  echo "DB_PASSWORD: $DB_PASSWORD"
  echo "DB_NAME: $DB_NAME"
  INSTANCE_CONNECTION_NAME=$(gcloud sql instances describe "$DB_INSTANCE_NAME" --project="$GCP_PROJECT_ID" --format="value(connectionName)")
  echo "INSTANCE_CONNECTION_NAME: $INSTANCE_CONNECTION_NAME (for Cloud Run -> Cloud SQL connection)"
  echo "For DATABASE_URL, use: postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${INSTANCE_CONNECTION_NAME}"
  echo ""
  DATABASE_URL_ENV_VAR="postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${INSTANCE_CONNECTION_NAME}"

  # Grant Cloud Run SA access to Cloud SQL instance
  echo "Granting Cloud Run service account ($SERVICE_ACCOUNT_EMAIL) 'Cloud SQL Client' role for DB connection..."
   gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
     --member="serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
     --role="roles/cloudsql.client"
else
  echo "Cloud SQL setup skipped. You will need to provide the DATABASE_URL environment variable manually."
  DATABASE_URL_ENV_VAR="postgresql://user:password@host:port/dbname" # Placeholder
fi

# --- 6. IAM Permissions for Service Account ---
# (Some permissions were granted contextually above, like on GCS bucket and Cloud SQL Client)
echo ""
echo "--- Section 6: Granting Additional IAM Permissions to Service Account ---"
echo "Granting '$SERVICE_ACCOUNT_EMAIL' necessary roles for Vertex AI and other services..."
# Vertex AI User: To make predictions (Imagen, Veo)
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
  --member="serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
  --role="roles/aiplatform.user"

# Service Account Token Creator: If any service needs to impersonate or create tokens (e.g., for Signed URLs if used)
# This might not be strictly needed if ADC handles everything for GCS client, but good for general purpose.
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
  --member="serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator"

echo "Core IAM permissions granted to service account."

# --- 7. Application Configuration File (.env) ---
echo ""
echo "--- Section 7: Generating .env Configuration File ---"
echo "Please provide the following values for your application's .env file."
echo "A strong, unique SECRET_KEY will be generated for you."

DJANGO_SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(50))')
read -p "Enter allowed hosts for Django (comma-separated, e.g., *.run.app,yourdomain.com): " ALLOWED_HOSTS_ENV_VAR
read -p "Enter CSRF trusted origins for Django (comma-separated HTTPS URLs, e.g., https://*.run.app,https://yourdomain.com): " CSRF_TRUSTED_ORIGINS_ENV_VAR

# GOOGLE_API_KEY_ENV_VAR will only be used if your app strictly needs it for Imagen 3 via google-genai
# The goal is to rely on the Service Account and ADC for Vertex AI services.
read -p "Enter your GOOGLE_API_KEY (if needed for Imagen 3 SDK, otherwise leave blank to rely on ADC): " GOOGLE_API_KEY_INPUT

echo "Creating .env file..."
cat << EOF > .env
# Django Settings
SECRET_KEY='${DJANGO_SECRET_KEY}'
DEBUG=False
ALLOWED_HOSTS='${ALLOWED_HOSTS_ENV_VAR}'
CSRF_TRUSTED_ORIGINS='${CSRF_TRUSTED_ORIGINS_ENV_VAR}'

# Database (Cloud SQL via socket)
DATABASE_URL='${DATABASE_URL_ENV_VAR}'

# Google Cloud Settings
GOOGLE_PROJECT_ID='${GCP_PROJECT_ID}'
GOOGLE_LOCATION='${GCP_REGION}'
GCS_BUCKET_NAME='${GCS_BUCKET_NAME}'
GCS_OBJECT_PATH_PREFIX='${GCS_OBJECT_PATH_PREFIX_ENV_VAR}'
# If GOOGLE_API_KEY is provided and needed for google-genai client with VertexAI=true for Imagen3
# This should be stored securely, e.g., in Secret Manager and injected into Cloud Run.
# For simplicity here, if entered, it's put in .env but this is NOT ideal for production.
GOOGLE_API_KEY='${GOOGLE_API_KEY_INPUT}'

# Cloud Run sets PORT automatically
# DJANGO_SETTINGS_MODULE='media_studio_project.settings' # Usually set by Gunicorn or run command
EOF
echo ".env file created. Review it and store it securely. DO NOT commit it to Git if it contains secrets."
echo "For Cloud Run, these variables should be set as environment variables on the service or via Secret Manager."

# --- 8. Build and Deploy to Cloud Run (Guidance) ---
echo ""
echo "--- Section 8: Build and Deploy to Cloud Run ---"
echo "You have a Dockerfile in your project. You'll need to build and push this image"
echo "to Google Artifact Registry or Container Registry, then deploy to Cloud Run."
CLOUD_RUN_SERVICE_NAME="media-studio-app" # Suggest a name
read -p "Enter a name for your Cloud Run service (default: '$CLOUD_RUN_SERVICE_NAME'): " CLOUD_RUN_SERVICE_NAME_INPUT
CLOUD_RUN_SERVICE_NAME=${CLOUD_RUN_SERVICE_NAME_INPUT:-$CLOUD_RUN_SERVICE_NAME}

# Create an Artifact Registry Docker repository (if it doesn't exist)
ARTIFACT_REGISTRY_REPO="media-studio-images"
ARTIFACT_REGISTRY_LOCATION="$GCP_REGION" # Or your preferred AR location

if gcloud artifacts repositories describe "$ARTIFACT_REGISTRY_REPO" --location="$ARTIFACT_REGISTRY_LOCATION" --project="$GCP_PROJECT_ID" &>/dev/null; then
  echo "Artifact Registry repository '$ARTIFACT_REGISTRY_REPO' in '$ARTIFACT_REGISTRY_LOCATION' already exists."
else
  read -p "Create Artifact Registry Docker repository '$ARTIFACT_REGISTRY_REPO' in '$ARTIFACT_REGISTRY_LOCATION'? (yes/no): " CONFIRM_AR_REPO
  if [ "$CONFIRM_AR_REPO" == "yes" ]; then
    gcloud artifacts repositories create "$ARTIFACT_REGISTRY_REPO" \
      --repository-format=docker \
      --location="$ARTIFACT_REGISTRY_LOCATION" \
      --description="Docker repository for Media Studio App" \
      --project="$GCP_PROJECT_ID"
    echo "Artifact Registry repository created."
  else
    echo "Artifact Registry repository creation skipped. Ensure one exists."
  fi
fi

IMAGE_TAG="${ARTIFACT_REGISTRY_LOCATION}-docker.pkg.dev/${GCP_PROJECT_ID}/${ARTIFACT_REGISTRY_REPO}/${CLOUD_RUN_SERVICE_NAME}:latest"
echo ""
echo "--- Configuring Cloud Build Service Account Permissions ---"
# Get Project Number
PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT_ID" --format="value(projectNumber)")
if [ -z "$PROJECT_NUMBER" ]; then
    echo "ERROR: Could not retrieve project number for $GCP_PROJECT_ID."
    exit 1
fi
CLOUD_BUILD_SA="service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"

echo "The default Cloud Build service account is: $CLOUD_BUILD_SA"
echo "This account needs permissions to access GCS (for source code) and Artifact Registry (to push images)."

# Grant Cloud Build SA the "Cloud Build Service Account" role (includes necessary permissions)
# This is often granted by default when the API is enabled, but ensuring it is good.
echo "Ensuring Cloud Build service account has 'Cloud Build Service Account' role..."
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${CLOUD_BUILD_SA}" \
    --role="roles/cloudbuild.builds.builder" \
    --condition=None # Ensure no condition is accidentally applied here for this broad role
echo "'Cloud Build Service Account' role granted/verified for ${CLOUD_BUILD_SA}."

echo "Granting the Cloud Run service account ($SERVICE_ACCOUNT_EMAIL) 'Storage Object Admin' on gs://${GCS_BUCKET_NAME}..."
gsutil iam ch \
  "serviceAccount:${SERVICE_ACCOUNT_EMAIL}:objectAdmin" \
  "gs://${GCS_BUCKET_NAME}"
echo "Cloud Run service account granted Storage Object Admin on the bucket."

GCS_SOURCE_BUCKET_FOR_BUILD="${GCP_PROJECT_ID}_cloudbuild" # Default bucket used by `gcloud builds submit`
echo "Ensuring Cloud Build service account ($CLOUD_BUILD_SA) can read source code from gs://${GCS_SOURCE_BUCKET_FOR_BUILD}..."
# This bucket is auto-created by Cloud Build. Permissions are usually set, but good to ensure.
# Granting objectViewer should be sufficient for Cloud Build to read the source.
# The `roles/cloudbuild.builds.builder` on the project might already cover this.
# We can be explicit if needed:
gsutil iam ch \
  "serviceAccount:${CLOUD_BUILD_SA}:objectViewer" \
  "gs://${GCS_SOURCE_BUCKET_FOR_BUILD}" || echo "Warning: Could not set explicit permissions on gs://${GCS_SOURCE_BUCKET_FOR_BUILD} for Cloud Build SA. Default permissions may apply."

echo "Ensuring Cloud Build SA ($CLOUD_BUILD_SA) can write to this Artifact Registry repository..."
gcloud artifacts repositories add-iam-policy-binding "$ARTIFACT_REGISTRY_REPO" \
    --location="$ARTIFACT_REGISTRY_LOCATION" \
    --project="$GCP_PROJECT_ID" \
    --member="serviceAccount:${CLOUD_BUILD_SA}" \
    --role="roles/artifactregistry.writer"
echo "Cloud Build SA granted Artifact Registry Writer role for this repository."

COMPUTE_ENGINE_DEFAULT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" # Default Compute Engine SA

# PRECAUTIONARY STEP: As per recent errors, also grant Compute Engine Default SA
# "Storage Object Viewer" on the Cloud Build source bucket. This is often needed if
# Cloud Build, for some reason, defaults to using it or if permissions are very strict.
CLOUD_BUILD_SOURCE_BUCKET="${GCP_PROJECT_ID}_cloudbuild"
echo "Granting 'Storage Object Viewer' to Compute Engine Default SA (${COMPUTE_ENGINE_DEFAULT_SA}) on gs://${CLOUD_BUILD_SOURCE_BUCKET} as a precaution..."
gsutil iam ch \
  "serviceAccount:${COMPUTE_ENGINE_DEFAULT_SA}:objectViewer" \
  "gs://${CLOUD_BUILD_SOURCE_BUCKET}" || echo "Warning: Could not set explicit 'objectViewer' permission on gs://${CLOUD_BUILD_SOURCE_BUCKET} for ${COMPUTE_ENGINE_DEFAULT_SA}. This might be okay if broader permissions exist or the bucket doesn't exist yet (Cloud Build creates it)."
echo "Precautionary 'Storage Object Viewer' grant attempted for ${COMPUTE_ENGINE_DEFAULT_SA}."
echo "Ideally, Cloud Build should use its dedicated SA ($CLOUD_BUILD_SA) which already has necessary permissions via 'roles/cloudbuild.builds.builder'."

# Grant Logs Writer to the service account Cloud Build is actually using (from error message)
echo "Ensuring the service account used by this build (${COMPUTE_ENGINE_DEFAULT_SA}) can write logs..."
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${COMPUTE_ENGINE_DEFAULT_SA}" \
    --role="roles/logging.logWriter" \
    --condition=None
echo "'Logs Writer' role granted to ${COMPUTE_ENGINE_DEFAULT_SA}."

gcloud artifacts repositories add-iam-policy-binding "${ARTIFACT_REGISTRY_REPO}" \
    --location="${ARTIFACT_REGISTRY_LOCATION}" \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:${COMPUTE_ENGINE_SA}" \
    --role="roles/artifactregistry.writer"

echo "Granted 'Artifact Registry Writer' role to ${COMPUTE_ENGINE_SA} on repository ${ARTIFACT_REGISTRY_REPO}."


echo ""
echo "Next steps for deployment (execute these commands in your project root where Dockerfile is):"
echo "1. Build the Docker image:"
echo "   gcloud builds submit --tag \"${IMAGE_TAG}\" . --project=\"${GCP_PROJECT_ID}\""
echo ""
echo "2. Deploy to Cloud Run (replace placeholders in <>):"
echo "   gcloud run deploy \"${CLOUD_RUN_SERVICE_NAME}\" \\"
echo "     --image=\"${IMAGE_TAG}\" \\"
echo "     --platform=managed \\"
echo "     --region=\"${GCP_REGION}\" \\"
echo "     --service-account=\"${SERVICE_ACCOUNT_EMAIL}\" \\"
echo "     --allow-unauthenticated \\" # Or --no-allow-unauthenticated and set up IAM/IAP
echo "     --set-env-vars=\"DJANGOSECRET_KEY=${DJANGO_SECRET_KEY}',DEBUG=False,DJANGO_ALLOWED_HOSTS='${ALLOWED_HOSTS_ENV_VAR}',DJANGO_CSRF_TRUSTED_ORIGINS='${CSRF_TRUSTED_ORIGINS_ENV_VAR}',DATABASE_URL=${DATABASE_URL_ENV_VAR},GOOGLE_PROJECT_ID=${GCP_PROJECT_ID},GOOGLE_LOCATION=${GCP_REGION},GCS_BUCKET_NAME=${GCS_BUCKET_NAME},GCS_OBJECT_PATH_PREFIX=${GCS_OBJECT_PATH_PREFIX_ENV_VAR}\" \\" # Add GOOGLE_API_KEY if needed
echo "     --add-cloudsql-instances=\"${INSTANCE_CONNECTION_NAME}\" \\" # If Cloud SQL was set up
echo "     --project=\"${GCP_PROJECT_ID}\""
echo ""
echo "   NOTE: For environment variables like SECRET_KEY, DATABASE_URL, and GOOGLE_API_KEY,"
echo "   it's STRONGLY recommended to use Google Secret Manager and link secrets to Cloud Run"
echo "   instead of passing them directly in --set-env-vars for production."
echo "   Example for one secret:"
echo "   echo -n \"<your-secret-value>\" | gcloud secrets create my-secret --data-file=- --project=\"${GCP_PROJECT_ID}\""
echo "   Then in 'gcloud run deploy', use: --update-secrets=MY_ENV_VAR=my-secret:latest"

# --- 9. Post-Deployment ---
echo ""
echo "--- Section 9: Post-Deployment Steps ---"
echo "1. After deployment, get your Cloud Run service URL from the output."
echo "2. Update your DJANGO_ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS in your .env or Cloud Run environment (YAML) variables to include this URL."
echo "   (You might need to redeploy if changing env vars on Cloud Run)."
echo "3. Run Django Migrations: The first time you deploy, or if you have database schema changes, migrations need to be run."
echo "   This is typically done by configuring your Docker CMD to run migrations before starting Gunicorn,"
echo "   or by manually running them via a Cloud Run job or by connecting to the DB proxy."
echo "   A common entrypoint script in Docker might look like:"
echo "   #!/bin/sh"
echo "   python manage.py migrate --noinput"
echo "   exec gunicorn media_studio_project.wsgi:application --bind 0.0.0.0:\$PORT --workers 2 --threads 4 --worker-class gthread"
echo ""
echo "----------------------------------------------------------------------"
echo " GCP Environment Setup Script Finished."
echo "----------------------------------------------------------------------"
echo "Review the output, especially any generated credentials or URLs."
echo "Remember to manage secrets like Django SECRET_KEY and Database Passwords securely."