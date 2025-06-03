#!/bin/bash

# Script to set up and deploy the Media Studio Django App on Google Cloud Platform.
# This script will make changes to the specified GCP project. Please review carefully.

set -e # Exit immediately if a command exits with a non-zero status.

echo "----------------------------------------------------------------------"
echo " Media Studio GCP Environment Setup & Deployment Script"
echo "----------------------------------------------------------------------"
echo "This script will guide you through configuring GCP resources and deploying"
echo "the application to Google Cloud Run."
echo ""
echo "Prerequisites:"
echo "  1. 'gcloud' CLI installed and authenticated with permissions on the target project."
echo "  2. Application source code (including Dockerfile) in the current directory."
echo ""
read -p "Do you wish to continue with the setup and deployment? (yes/no): " CONFIRM_CONTINUE
if [ "$CONFIRM_CONTINUE" != "yes" ]; then
  echo "Setup aborted."
  exit 1
fi

# --- 1. Gather GCP Project and Region Information ---
echo ""
echo "--- Section 1: GCP Project and Region Configuration ---"
CURRENT_PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
read -p "Enter your Google Cloud Project ID [default: $CURRENT_PROJECT_ID]: " GCP_PROJECT_ID
GCP_PROJECT_ID=${GCP_PROJECT_ID:-$CURRENT_PROJECT_ID}
if [ -z "$GCP_PROJECT_ID" ]; then echo "ERROR: GCP Project ID is required."; exit 1; fi
gcloud config set project "$GCP_PROJECT_ID"
echo "Using project: $GCP_PROJECT_ID"

CURRENT_REGION=$(gcloud config get-value run/region 2>/dev/null)
DEFAULT_REGION="us-central1"
read -p "Enter GCP region for services (e.g., us-central1) [default: $DEFAULT_REGION]: " GCP_REGION
GCP_REGION=${GCP_REGION:-$DEFAULT_REGION}
gcloud config set run/region "$GCP_REGION" # Sets default for 'gcloud run' commands
echo "Using region: $GCP_REGION"

# --- 2. Enable Necessary Google Cloud APIs ---
echo ""
echo "--- Section 2: Enabling Google Cloud APIs ---"
REQUIRED_APIS=(
    "run.googleapis.com" "aiplatform.googleapis.com" "storage.googleapis.com"
    "artifactregistry.googleapis.com" "iam.googleapis.com" "sqladmin.googleapis.com"
    "cloudbuild.googleapis.com" "cloudresourcemanager.googleapis.com" "iamcredentials.googleapis.com"
    "secretmanager.googleapis.com"
)
echo "The following APIs will be enabled if not already: ${REQUIRED_APIS[*]}"
read -p "Proceed? (yes/no): " CONFIRM_APIS
if [ "$CONFIRM_APIS" == "yes" ]; then
  gcloud services enable "${REQUIRED_APIS[@]}" --project="$GCP_PROJECT_ID"
  echo "APIs enabled."
else
  echo "API enablement skipped. Deployment may fail."
fi

# --- 3. Create and Configure Service Account for Cloud Run ---
echo ""
echo "--- Section 3: Cloud Run Service Account Setup ---"
CR_SA_NAME="media-studio-run-sa" # Cloud Run Service Account Name
CR_SA_EMAIL="${CR_SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$CR_SA_EMAIL" --project="$GCP_PROJECT_ID" &>/dev/null; then
  echo "Cloud Run service account $CR_SA_EMAIL already exists."
else
  gcloud iam service-accounts create "$CR_SA_NAME" --display-name="Media Studio Cloud Run SA" --project="$GCP_PROJECT_ID"
  echo "Created Cloud Run service account: $CR_SA_EMAIL"
fi

echo "Granting necessary IAM roles to Cloud Run SA ($CR_SA_EMAIL):"
ROLES_CR_SA=(
    "roles/aiplatform.user"                 # For Vertex AI (Imagen, Veo)
    "roles/iam.serviceAccountTokenCreator"  # For signing GCS URLs
    "roles/cloudsql.client"                 # For Cloud SQL connection
    # Storage Object Admin will be granted per-bucket later
)
for role in "${ROLES_CR_SA[@]}"; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${CR_SA_EMAIL}" --role="$role" --condition=None
  echo "  - Granted $role"
done

# --- 4. Configure Cloud Build Service Account Permissions ---
echo ""
echo "--- Section 4: Cloud Build Service Account Permissions ---"
PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT_ID" --format="value(projectNumber)")
if [ -z "$PROJECT_NUMBER" ]; then echo "ERROR: Could not retrieve project number for $GCP_PROJECT_ID."; exit 1; fi

CB_SA_DEDICATED="service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
CB_SA_COMPUTE_DEFAULT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "Ensuring dedicated Cloud Build SA (${CB_SA_DEDICATED}) has 'Cloud Build Service Account' and 'Artifact Registry Writer' roles..."
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="serviceAccount:${CB_SA_DEDICATED}" --role="roles/cloudbuild.builds.builder" --condition=None
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="serviceAccount:${CB_SA_DEDICATED}" --role="roles/artifactregistry.writer" --condition=None
echo "  - Roles granted to ${CB_SA_DEDICATED}."

echo "Applying precautionary permissions for Compute Engine Default SA (${CB_SA_COMPUTE_DEFAULT}) if used by Cloud Build:"
CB_SOURCE_BUCKET_NAME="${GCP_PROJECT_ID}_cloudbuild" # Default GCS bucket for Cloud Build sources
echo "  - Granting 'Storage Object Viewer' on gs://${CB_SOURCE_BUCKET_NAME}..."
gsutil iam ch "serviceAccount:${CB_SA_COMPUTE_DEFAULT}:objectViewer" "gs://${CB_SOURCE_BUCKET_NAME}" || echo "    Warning: Could not set objectViewer on gs://${CB_SOURCE_BUCKET_NAME} for Compute SA. Default permissions may apply or bucket not yet auto-created by Cloud Build."
echo "  - Granting 'Logs Writer'..."
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="serviceAccount:${CB_SA_COMPUTE_DEFAULT}" --role="roles/logging.logWriter" --condition=None
echo "  - Granting 'Artifact Registry Writer'..."
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="serviceAccount:${CB_SA_COMPUTE_DEFAULT}" --role="roles/artifactregistry.writer" --condition=None
echo "Precautionary permissions for Compute Engine Default SA applied."

# --- 5. Google Cloud Storage Bucket Setup ---
echo ""
echo "--- Section 5: Google Cloud Storage Bucket Setup ---"
DEFAULT_GCS_BUCKET_NAME="${GCP_PROJECT_ID}-media-studio-assets"
read -p "Enter GCS bucket name for media assets [default: '$DEFAULT_GCS_BUCKET_NAME']: " GCS_BUCKET_NAME
GCS_BUCKET_NAME=${GCS_BUCKET_NAME:-$DEFAULT_GCS_BUCKET_NAME}

if ! gsutil ls "gs://${GCS_BUCKET_NAME}" &>/dev/null; then
  gsutil mb -p "$GCP_PROJECT_ID" -l "$GCP_REGION" -b on "gs://${GCS_BUCKET_NAME}" # Enable Uniform Bucket-Level Access
  echo "Created GCS Bucket: gs://${GCS_BUCKET_NAME}"
else
  echo "GCS Bucket gs://${GCS_BUCKET_NAME} already exists."
fi
echo "Granting Cloud Run SA (${CR_SA_EMAIL}) 'Storage Object Admin' on gs://${GCS_BUCKET_NAME}..."
gsutil iam ch "serviceAccount:${CR_SA_EMAIL}:objectAdmin" "gs://${GCS_BUCKET_NAME}"
echo "  - Cloud Run SA granted 'Storage Object Admin' on the bucket."
GCS_OBJECT_PATH_PREFIX_ENV_VAR="media_studio_app_data/" # Standardized prefix for .env and settings.py

# --- 6. Cloud SQL (PostgreSQL) Database Setup (Optional) ---
echo ""
echo "--- Section 6: Cloud SQL (PostgreSQL) Database Setup (Optional) ---"
DB_INSTANCE_NAME="media-studio-pg-db"
DB_NAME="mediastudio_main"
DB_USER="mediastudio_app_user"
DATABASE_URL_ENV_VAR_VALUE="" # Initialize
INSTANCE_CONNECTION_NAME_ENV_VAR_VALUE=""

read -p "Set up a NEW Cloud SQL PostgreSQL instance '$DB_INSTANCE_NAME'? (yes/no - 'no' requires manual DB config for DATABASE_URL): " CONFIRM_DB_SETUP
if [ "$CONFIRM_DB_SETUP" == "yes" ]; then
  if gcloud sql instances describe "$DB_INSTANCE_NAME" --project="$GCP_PROJECT_ID" &>/dev/null; then
    echo "WARNING: Cloud SQL instance '$DB_INSTANCE_NAME' already exists. This script will NOT modify it."
    echo "Attempting to retrieve its connection name. You may need to ensure DB '$DB_NAME' and user '$DB_USER' exist on it."
    INSTANCE_CONNECTION_NAME_ENV_VAR_VALUE=$(gcloud sql instances describe "$DB_INSTANCE_NAME" --project="$GCP_PROJECT_ID" --format="value(connectionName)")
    read -p "Enter the existing database user for instance '$DB_INSTANCE_NAME': " DB_USER_INPUT
    DB_USER=${DB_USER_INPUT:-$DB_USER}
    read -s -p "Enter password for database user '$DB_USER_INPUT': " DB_PASSWORD_INPUT
    echo ""
    DB_PASSWORD=$DB_PASSWORD_INPUT
  else
    echo "Creating new Cloud SQL instance '$DB_INSTANCE_NAME' (this may take several minutes)..."
    gcloud sql instances create "$DB_INSTANCE_NAME" \
      --database-version=POSTGRES_15 \
      --tier=db-f1-micro \
      --region="$GCP_REGION" \
      --project="$GCP_PROJECT_ID"
    echo "Created Cloud SQL instance: $DB_INSTANCE_NAME"
    INSTANCE_CONNECTION_NAME_ENV_VAR_VALUE=$(gcloud sql instances describe "$DB_INSTANCE_NAME" --project="$GCP_PROJECT_ID" --format="value(connectionName)")
    
    echo "Creating database '$DB_NAME'..."
    gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE_NAME" --project="$GCP_PROJECT_ID"
    echo "Created database: $DB_NAME"

    DB_PASSWORD=$(openssl rand -base64 20 | tr -dc 'a-zA-Z0-9!@#$%^&*' | fold -w 20 | head -n 1)
    echo "Creating database user '$DB_USER' with a generated password..."
    gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE_NAME" --password="$DB_PASSWORD" --project="$GCP_PROJECT_ID"
    echo "Created DB user: $DB_USER (Password: $DB_PASSWORD <-- IMPORTANT: SAVE THIS SECURELY!)"
  fi
  
  if [ -n "$DB_USER" ] && [ -n "$DB_PASSWORD" ] && [ -n "$DB_NAME" ] && [ -n "$INSTANCE_CONNECTION_NAME_ENV_VAR_VALUE" ]; then
    DATABASE_URL_ENV_VAR_VALUE="postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${INSTANCE_CONNECTION_NAME_ENV_VAR_VALUE}"
    echo "  DATABASE_URL (for Secret Manager): $DATABASE_URL_ENV_VAR_VALUE"
  else
     echo "Could not determine all necessary DB connection details. Manual DATABASE_URL configuration will be needed."
     DATABASE_URL_ENV_VAR_VALUE="YOUR_MANUAL_DATABASE_URL_HERE" # Placeholder
  fi
else
  echo "Cloud SQL setup skipped. You must provide the DATABASE_URL for Cloud Run environment or Secret Manager."
  DATABASE_URL_ENV_VAR_VALUE="YOUR_MANUAL_DATABASE_URL_HERE" # Placeholder
fi


# --- 7. Artifact Registry Repository Setup ---
echo ""
echo "--- Section 7: Artifact Registry Repository Setup ---"
CLOUD_RUN_SERVICE_NAME_DEFAULT="media-studio-app" 
read -p "Enter a name for your Cloud Run service (used for image tagging) [default: '${CLOUD_RUN_SERVICE_NAME_DEFAULT}']: " CLOUD_RUN_SERVICE_NAME_INPUT
CLOUD_RUN_SERVICE_NAME=${CLOUD_RUN_SERVICE_NAME_INPUT:-$CLOUD_RUN_SERVICE_NAME_DEFAULT}

AR_REPO="media-studio-images"
AR_LOCATION="$GCP_REGION" # Use the same region for AR
if ! gcloud artifacts repositories describe "$AR_REPO" --location="$AR_LOCATION" --project="$GCP_PROJECT_ID" &>/dev/null; then
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$AR_LOCATION" --description="Media Studio Docker Images" --project="$GCP_PROJECT_ID"
  echo "Created Artifact Registry repository: $AR_REPO"
else
  echo "Artifact Registry repository $AR_REPO already exists."
fi
# Ensure Cloud Build SAs can write to this specific AR repo (already done broadly, this is belt-and-suspenders for the specific repo)
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$AR_LOCATION" --project="$GCP_PROJECT_ID" --member="serviceAccount:${CB_SA_DEDICATED}" --role="roles/artifactregistry.writer" --condition=None
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$AR_LOCATION" --project="$GCP_PROJECT_ID" --member="serviceAccount:${CB_SA_COMPUTE_DEFAULT}" --role="roles/artifactregistry.writer" --condition=None
echo "Artifact Registry permissions re-verified for build service accounts for repository $AR_GEO."

IMAGE_TAG_URL="${AR_LOCATION}-docker.pkg.dev/${GCP_PROJECT_ID}/${AR_REPO}/${CLOUD_RUN_SERVICE_NAME}:latest"

# --- 8. Prepare Secrets in Google Secret Manager ---
echo ""
echo "--- Section 8: Preparing Secrets in Google Secret Manager ---"
echo "Sensitive configurations will be stored in Secret Manager."

# Django Secret Key
DJANGO_SECRET_KEY_VALUE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')
DJANGO_SECRET_KEY_NAME="media-studio-django-secret-key" # Secret Name
echo "Creating/Updating Django SECRET_KEY in Secret Manager as '$DJANGO_SECRET_KEY_NAME'..."
echo -n "$DJANGO_SECRET_KEY_VALUE" | gcloud secrets create "$DJANGO_SECRET_KEY_NAME" \
    --project="$GCP_PROJECT_ID" --replication-policy=automatic --data-file=- \
    --format="value(name)" || \
gcloud secrets versions add "$DJANGO_SECRET_KEY_NAME" --project="$GCP_PROJECT_ID" --data-file=- <<< "$DJANGO_SECRET_KEY_VALUE" \
    --format="value(name)"
gcloud secrets add-iam-policy-binding "$DJANGO_SECRET_KEY_NAME" \
    --project="$GCP_PROJECT_ID" --member="serviceAccount:${CR_SA_EMAIL}" --role="roles/secretmanager.secretAccessor" --condition=None
echo "  - Django SECRET_KEY stored and Cloud Run SA granted access."

# Database URL
DB_URL_SECRET_NAME="media-studio-database-url"
if [ -n "$DATABASE_URL_ENV_VAR_VALUE" ] && [ "$DATABASE_URL_ENV_VAR_VALUE" != "YOUR_MANUAL_DATABASE_URL_HERE" ]; then
    echo "Creating/Updating DATABASE_URL in Secret Manager as '$DB_URL_SECRET_NAME'..."
    echo -n "$DATABASE_URL_ENV_VAR_VALUE" | gcloud secrets create "$DB_URL_SECRET_NAME" \
        --project="$GCP_PROJECT_ID" --replication-policy=automatic --data-file=- \
        --format="value(name)" || \
    gcloud secrets versions add "$DB_URL_SECRET_NAME" --project="$GCP_PROJECT_ID" --data-file=- <<< "$DATABASE_URL_ENV_VAR_VALUE" \
        --format="value(name)"
    gcloud secrets add-iam-policy-binding "$DB_URL_SECRET_NAME" \
        --project="$GCP_PROJECT_ID" --member="serviceAccount:${CR_SA_EMAIL}" --role="roles/secretmanager.secretAccessor" --condition=None
    echo "  - DATABASE_URL stored and Cloud Run SA granted access."
else
    echo "WARNING: DATABASE_URL was not automatically configured by the script."
    echo "         You MUST create a secret named '$DB_URL_SECRET_NAME' with the correct DATABASE_URL value"
    echo "         and grant the Cloud Run SA ('${CR_SA_EMAIL}') 'Secret Manager Secret Accessor' role on it."
fi

# Optional GOOGLE_API_KEY
GOOGLE_API_KEY_SECRET_NAME="media-studio-google-api-key"
SECRET_MAPPINGS_API_KEY=""
read -p "Enter your GOOGLE_API_KEY IF it's strictly required by the 'google-genai' SDK for Imagen 3 (otherwise, leave blank to rely on ADC for Vertex AI): " GOOGLE_API_KEY_INPUT
if [ -n "$GOOGLE_API_KEY_INPUT" ]; then
    echo "Creating/Updating GOOGLE_API_KEY in Secret Manager as '$GOOGLE_API_KEY_SECRET_NAME'..."
    echo -n "$GOOGLE_API_KEY_INPUT" | gcloud secrets create "$GOOGLE_API_KEY_SECRET_NAME" \
        --project="$GCP_PROJECT_ID" --replication-policy=automatic --data-file=- \
        --format="value(name)" || \
    gcloud secrets versions add "$GOOGLE_API_KEY_SECRET_NAME" --project="$GCP_PROJECT_ID" --data-file=- <<< "$GOOGLE_API_KEY_INPUT" \
        --format="value(name)"
    gcloud secrets add-iam-policy-binding "$GOOGLE_API_KEY_SECRET_NAME" \
        --project="$GCP_PROJECT_ID" --member="serviceAccount:${CR_SA_EMAIL}" --role="roles/secretmanager.secretAccessor" --condition=None
    echo "  - GOOGLE_API_KEY stored and Cloud Run SA granted access."
    SECRET_MAPPINGS_API_KEY=",GOOGLE_API_KEY=${GOOGLE_API_KEY_SECRET_NAME}:latest"
fi
echo "Secrets preparation complete."


# --- 9. Build Docker Image ---
echo ""
echo "--- Section 9: Building Docker Image via Google Cloud Build ---"
echo "This will use the Dockerfile in the current directory."
read -p "Proceed with building the Docker image and pushing to Artifact Registry ('${IMAGE_TAG_URL}')? (yes/no): " CONFIRM_BUILD
if [ "$CONFIRM_BUILD" == "yes" ]; then
  gcloud builds submit --tag "${IMAGE_TAG_URL}" . --project="${GCP_PROJECT_ID}"
  echo "Docker image built and pushed successfully: ${IMAGE_TAG_URL}"
else
  echo "Docker image build skipped. You must build and push it manually to ${IMAGE_TAG_URL} before deploying."
  exit 1 # Cannot deploy without an image
fi

# --- 10. Deploy to Cloud Run ---
echo ""
echo "--- Section 10: Deploying Application to Google Cloud Run ---"
# Initial permissive values, will be updated.
INITIAL_ALLOWED_HOSTS_FOR_DEPLOY="'*'" # Quoted for gcloud command
INITIAL_CSRF_ORIGINS_FOR_DEPLOY="''"    # Empty string

ENV_VARS_CLOUDRUN="DJANGO_DEBUG=False,GOOGLE_PROJECT_ID=${GCP_PROJECT_ID},GOOGLE_LOCATION=${GCP_REGION},GCS_BUCKET_NAME=${GCS_BUCKET_NAME},GCS_OBJECT_PATH_PREFIX=${GCS_OBJECT_PATH_PREFIX_ENV_VAR},DJANGO_ENV=production,DJANGO_ALLOWED_HOSTS=${INITIAL_ALLOWED_HOSTS_FOR_DEPLOY},DJANGO_CSRF_TRUSTED_ORIGINS=${INITIAL_CSRF_ORIGINS_FOR_DEPLOY},IMAGEN_ADD_WATERMARK=False"
SECRET_MAPPINGS_CLOUDRUN="DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY_NAME}:latest,DATABASE_URL=${DB_URL_SECRET_NAME}:latest${SECRET_MAPPINGS_API_KEY}"

SQL_INSTANCE_FLAG_DEPLOY=""
if [ "$CONFIRM_DB_SETUP" == "yes" ] && [ -n "$INSTANCE_CONNECTION_NAME_ENV_VAR_VALUE" ]; then
    SQL_INSTANCE_FLAG_DEPLOY="--add-cloudsql-instances=\"${INSTANCE_CONNECTION_NAME_ENV_VAR_VALUE}\""
fi

echo "Deploying Cloud Run service: $CLOUD_RUN_SERVICE_NAME with initial host settings..."
# Initial deployment to get the URL
gcloud run deploy "${CLOUD_RUN_SERVICE_NAME}" \
  --image="${IMAGE_TAG_URL}" \
  --platform=managed \
  --region="${GCP_REGION}" \
  --service-account="${CR_SA_EMAIL}" \
  --allow-unauthenticated \
  --port=8000 \
  --update-secrets="${SECRET_MAPPINGS_CLOUDRUN}" \
  --set-env-vars="${ENV_VARS_CLOUDRUN}" \
  ${SQL_INSTANCE_FLAG_DEPLOY} \
  --project="${GCP_PROJECT_ID}"

# Capture the deployed service URL
SERVICE_URL=$(gcloud run services describe "${CLOUD_RUN_SERVICE_NAME}" --platform=managed --region="${GCP_REGION}" --project="${GCP_PROJECT_ID}" --format="value(status.url)")

if [ -z "$SERVICE_URL" ]; then
    echo "ERROR: Could not retrieve Cloud Run service URL after initial deployment. Please check the GCP Console."
    echo "You will need to manually update DJANGO_ALLOWED_HOSTS and DJANGO_CSRF_TRUSTED_ORIGINS."
else
    echo ""
    echo "Cloud Run Service Deployed! URL: ${SERVICE_URL}"
    echo "--- Updating Service with Specific Host and CSRF Origin ---"
    
    SERVICE_HOSTNAME=$(echo "$SERVICE_URL" | sed -e 's|^[^/]*//||' -e 's|/.*$||')
    CSRF_SERVICE_ORIGIN="${SERVICE_URL}" # This is already https://...

    echo "Updating DJANGO_ALLOWED_HOSTS to: ${SERVICE_HOSTNAME}"
    echo "Updating DJANGO_CSRF_TRUSTED_ORIGINS to: ${CSRF_SERVICE_ORIGIN}"

    # Construct the final environment variables string for the update
    FINAL_ENV_VARS_CLOUDRUN="DJANGO_DEBUG=False,GOOGLE_PROJECT_ID=${GCP_PROJECT_ID},GOOGLE_LOCATION=${GCP_REGION},GCS_BUCKET_NAME=${GCS_BUCKET_NAME},GCS_OBJECT_PATH_PREFIX=${GCS_OBJECT_PATH_PREFIX_ENV_VAR},DJANGO_ENV=production,DJANGO_ALLOWED_HOSTS='${SERVICE_HOSTNAME}',DJANGO_CSRF_TRUSTED_ORIGINS='${CSRF_SERVICE_ORIGIN}'"
    if [ -n "$GOOGLE_API_KEY_INPUT" ] && [[ $SECRET_MAPPINGS_CLOUDRUN != *"GOOGLE_API_KEY"* ]]; then # If API key was set via env not secret
        FINAL_ENV_VARS_CLOUDRUN+=",GOOGLE_API_KEY=${GOOGLE_API_KEY_INPUT}"
    fi

    read -p "Proceed with updating the Cloud Run service with these specific host settings? (yes/no): " CONFIRM_HOST_UPDATE
    if [ "$CONFIRM_HOST_UPDATE" == "yes" ]; then
      gcloud run services update "${CLOUD_RUN_SERVICE_NAME}" \
        --platform=managed \
        --region="${GCP_REGION}" \
        --update-env-vars="${FINAL_ENV_VARS_CLOUDRUN}" \
        --project="${GCP_PROJECT_ID}"
      echo "Cloud Run service updated with specific host and CSRF origin settings."
    else
      echo "Service host settings not updated automatically."
      echo "PLEASE MANUALLY UPDATE DJANGO_ALLOWED_HOSTS and DJANGO_CSRF_TRUSTED_ORIGINS"
      echo "in your Cloud Run service configuration ('${CLOUD_RUN_SERVICE_NAME}') to:"
      echo "  DJANGO_ALLOWED_HOSTS='${SERVICE_HOSTNAME}'"
      echo "  DJANGO_CSRF_TRUSTED_ORIGINS='${CSRF_SERVICE_ORIGIN}'"
    fi
fi

# --- 11. Final Post-Deployment Instructions ---
echo ""
echo "--- Section 11: Final Post-Deployment Steps ---"
echo "1. Your application should be accessible at (URL, confirm from deploy output): ${SERVICE_URL}"
echo "2. If you didn't automatically update host settings, ensure DJANGO_ALLOWED_HOSTS and DJANGO_CSRF_TRUSTED_ORIGINS are correctly set in the Cloud Run service's environment variables."
echo "3. Ensure your Docker entrypoint script (or Docker CMD) runs Django migrations: 'python manage.py migrate --noinput' before starting Gunicorn."
echo "   If not, you may need to run migrations manually for the first deployment (e.g., via a one-off Cloud Run job or Cloud SQL Proxy)."
echo "   An example entrypoint.sh content:"
echo "     #!/bin/sh"
echo "     echo 'Running Django migrations...'"
echo "     python manage.py migrate --noinput"
echo "     echo 'Starting Gunicorn...'"
echo "     exec gunicorn media_studio_project.wsgi:application --bind 0.0.0.0:\$PORT --workers \$(nproc --all) --threads 2 --worker-class gthread"
echo "     (Make entrypoint.sh executable and set as CMD or ENTRYPOINT in Dockerfile)"
echo ""
echo "----------------------------------------------------------------------"
echo " GCP Environment Setup & Deployment Script Finished."
echo "----------------------------------------------------------------------"