# cleanroom-pipeline

#Before proceeding to create or run the workflow, below commands must be executed on their respective project

#To enable the google APIs

gcloud config set project project-123456789

gcloud services enable \
  analyticshub.googleapis.com \
  bigquery.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  cloudresourcemanager.googleapis.com



#Set the project to add IAM roles wrt SA
  
export PROJECT_ID="project-123456789"
export SA_EMAIL="gha-cleanroom-deployer@project-123456789.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/analyticshub.admin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.jobUser"
  
  


#Give the dataset level permission to access by the SA
  
export DATASET_ID="My_dataset_id"

bq show --format=prettyjson ${PROJECT_ID}:${DATASET_ID} > /tmp/ds.json
jq --arg sa "$SA_EMAIL" \
  '.access = ([.access[] | select(.userByEmail != $sa)] + [{"role":"OWNER","userByEmail":$sa}])' \
  /tmp/ds.json > /tmp/ds_new.json
bq update --dataset --source /tmp/ds_new.json ${PROJECT_ID}:${DATASET_ID}


#Verify it worked

bq show --format=prettyjson ${PROJECT_ID}:${DATASET_ID} | grep -B1 -A1 "$SA_EMAIL"
