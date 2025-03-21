# Databricks notebook source
# MAGIC %md-sandbox
# MAGIC # Building a Computer Vision model with hugging face
# MAGIC
# MAGIC <img src="https://github.com/databricks-demos/dbdemos-resources/blob/main/images/product/computer-vision/deeplearning-cv-pcb-flow-4.png?raw=true" width="700px" style="float: right"/>
# MAGIC
# MAGIC
# MAGIC Our next step as Data Scientist is to implement a ML model to run image segmentation.
# MAGIC
# MAGIC We'll re-use the gold table built in our previous data pipeline as training dataset.
# MAGIC
# MAGIC Building such a model is greatly simplified by using the <a href="https://huggingface.co/docs/transformers/index">huggingface transformer library</a>.
# MAGIC  
# MAGIC
# MAGIC ## MLOps steps
# MAGIC
# MAGIC While building an image segmentation model can be easily done, deploying such a model in production is much harder.
# MAGIC
# MAGIC Databricks simplifies this process and accelerates the Data Science journey with the help of MLFlow providing
# MAGIC
# MAGIC * Auto experimentation & tracking
# MAGIC * Simple, distributed hyperparameter tuning with hyperopt to get the best model
# MAGIC * Model packaging in MLFlow, abstracting our ML framework
# MAGIC * Model registry for governance
# MAGIC * Batch or real time serving (1 click deployment)
# MAGIC
# MAGIC <!-- Collect usage data (view). Remove it to disable collection. View README for more details.  -->
# MAGIC <img width="1px" src="https://www.google-analytics.com/collect?v=1&gtm=GTM-NKQ8TT7&tid=UA-163989034-1&cid=555&aip=1&t=event&ec=field_demos&ea=display&dp=%2F42_field_demos%2Ffeatures%2Fcomputer-vision-dl%2Fhf&dt=ML">

# COMMAND ----------

# MAGIC %pip install databricks-sdk==0.39.0 datasets==2.20.0 transformers==4.49.0 tf-keras==2.17.0 accelerate==1.4.0 mlflow==2.20.2 torchvision==0.20.1 deepspeed==0.14.4 evaluate==0.4.3
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Demo Initialization
# MAGIC %run ./_resources/00-init $reset_all_data=false

# COMMAND ----------

# DBTITLE 1,Review our training dataset
#Setup the training experiment
DBDemos.init_experiment_for_batch("computer-vision-dl", "pcb")

df = spark.read.table("training_dataset_augmented")
display(df.limit(10))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create our Dataset from the delta table
# MAGIC
# MAGIC Hugging face makes this step very easy. All it takes is calling the `Dataset.from_spark` function. 
# MAGIC
# MAGIC Read the <a href="https://www.databricks.com/blog/contributing-spark-loader-for-hugging-face-datasets">blogbost</a> for more detail on the new Delta Loader.

# COMMAND ----------

# DBTITLE 1,Create the transformer dataset from a spark dataframe (Delta Table)   
from datasets import Dataset

#Note: from_spark support coming with serverless compute - we'll use from_pandas for this simple demo having a small dataset
#dataset = Dataset.from_spark(df), cache_dir="/tmp/hf_cache/train").rename_column("content", "image")
dataset = Dataset.from_pandas(df.toPandas()).rename_column("content", "image")

splits = dataset.train_test_split(test_size=0.2, seed = 42)
train_ds = splits['train']
val_ds = splits['test']

# COMMAND ----------

# MAGIC %md 
# MAGIC ## Transfer learning with Hugging Face
# MAGIC
# MAGIC Transfer learning is the process of taking an existing model trained for another task on thousands of images, and transfering its knowledge to our domain. Hugging Face provides a helper class to make transfer learning very easy to implement.
# MAGIC
# MAGIC
# MAGIC The classic process is to re-train the model or part of the model (typically the last layer) using our custom dataset.
# MAGIC
# MAGIC This provides an the best tradeoff between training cost and efficiency, especially when our training dataset is limited.

# COMMAND ----------

# DBTITLE 1,Define the base model
import torch
from transformers import AutoFeatureExtractor, AutoImageProcessor

# pre-trained model from which to fine-tune
# Check the hugging face repo for more details & models: https://huggingface.co/google/vit-base-patch16-224
model_checkpoint = "google/vit-base-patch16-224"

#Check GPU availability
if not torch.cuda.is_available(): # is gpu
  #Use a smaller model for cpu demo
  model_checkpoint = "WinKawaks/vit-tiny-patch16-224" 
  print("Please use a GPU-cluster for model training, CPU instances will be too slow. In serverless, open the Environement tab and select a GPU (might require a preview).")

# COMMAND ----------

# DBTITLE 1,Define image transformations for training & validation
from PIL import Image
import io
from torchvision.transforms import CenterCrop, Compose, Normalize, RandomResizedCrop, Resize, ToTensor, Lambda

#Extract the model features (contains info on the pre-process step required to transform our data, such as resizing & normalization)
#Using the model parameters makes it easy to switch to another model without any change, even if the input size is different.
model_def = AutoFeatureExtractor.from_pretrained(model_checkpoint)

normalize = Normalize(mean=model_def.image_mean, std=model_def.image_std)
byte_to_pil = Lambda(lambda b: Image.open(io.BytesIO(b)).convert("RGB"))

#Transformations on our training dataset. we'll add some crop here
train_transforms = Compose([byte_to_pil,
                            RandomResizedCrop((model_def.size['height'], model_def.size['width'])),
                            ToTensor(), #convert the PIL img to a tensor
                            normalize
                           ])
#Validation transformation, we only resize the images to the expected size
val_transforms = Compose([byte_to_pil,
                          Resize((model_def.size['height'], model_def.size['width'])),
                          ToTensor(),  #convert the PIL img to a tensor
                          normalize
                         ])

# Add some random resizing & transformation to our training dataset
def preprocess_train(batch):
    """Apply train_transforms across a batch."""
    batch["image"] = [train_transforms(image) for image in batch["image"]]
    return batch

# Validation dataset
def preprocess_val(batch):
    """Apply val_transforms across a batch."""
    batch["image"] = [val_transforms(image) for image in batch["image"]]
    return batch
  
#Set our training / validation transformations
train_ds.set_transform(preprocess_train)
val_ds.set_transform(preprocess_val)

# COMMAND ----------

# DBTITLE 1,Build our model from the pretrained model
from transformers import AutoModelForImageClassification, TrainingArguments, Trainer

#Mapping between class label and value (huggingface use it during inference to output the proper label)
label2id, id2label = dict(), dict()
for i, label in enumerate(set(dataset['label'])):
    label2id[label] = i
    id2label[i] = label
    
model = AutoModelForImageClassification.from_pretrained(
    model_checkpoint, 
    label2id=label2id,
    id2label=id2label,
    ignore_mismatched_sizes = True # provide this in case you're planning to fine-tune an already fine-tuned checkpoint
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fine tuning our model 
# MAGIC
# MAGIC Our dataset and model is ready. We can now start the training step to fine-tune the model.
# MAGIC
# MAGIC *Note that for production-grade use cases, we would typically to do some [hyperparameter](https://huggingface.co/docs/transformers/hpo_train) tuning here. We'll keep it simple for this first example and run it with fixed settings.*
# MAGIC

# COMMAND ----------

# DBTITLE 1,Training parameters
model_name = model_checkpoint.split("/")[-1]
batch_size = 32  # batch size for training and evaluation

args = TrainingArguments(
    f"/tmp/huggingface/pcb/{model_name}-finetuned-leaf",
    remove_unused_columns=False,
    evaluation_strategy="epoch",
    save_strategy="epoch",
    learning_rate=5e-5,
    per_device_train_batch_size=batch_size,
    gradient_accumulation_steps=1,
    per_device_eval_batch_size=batch_size,
    no_cuda=not torch.cuda.is_available(),  # Run on CPU for resnet to make it easier
    num_train_epochs=20, 
    warmup_ratio=0.1,
    logging_steps=10,
    load_best_model_at_end=True,
    metric_for_best_model="f1",
    push_to_hub=False
)

# COMMAND ----------

# DBTITLE 1,Define our evaluation metric
import numpy as np
import evaluate
# the compute_metrics function takes a Named Tuple as input:
# predictions, which are the logits of the model as Numpy arrays,
# and label_ids, which are the ground-truth labels as Numpy arrays.

# Let's evaluate our model against a F1 score. Keep it as binary for this demo (we don't classify by default type)
accuracy = evaluate.load("f1")

def compute_metrics(eval_pred):
    """Computes accuracy on a batch of predictions"""
    predictions = np.argmax(eval_pred.predictions, axis=1)
    return accuracy.compute(predictions=predictions, references=eval_pred.label_ids)

# COMMAND ----------

# DBTITLE 1,Start our Training and log the model to MLFlow
import mlflow
from mlflow.models.signature import infer_signature
import torch
from PIL import Image
from torchvision.transforms import ToPILImage
from transformers import pipeline, DefaultDataCollator, EarlyStoppingCallback

def collate_fn(examples):
    pixel_values = torch.stack([e["image"] for e in examples])
    labels = torch.tensor([label2id[e["label"]] for e in examples])
    return {"pixel_values": pixel_values, "labels": labels}

#Make sure the model is trained on GPU
device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
model.to(device)

with mlflow.start_run(run_name="hugging_face") as run:
  early_stop = EarlyStoppingCallback(early_stopping_patience=10)
  trainer = Trainer(
    model, 
    args, 
    train_dataset=train_ds, 
    eval_dataset=val_ds, 
    tokenizer=model_def, 
    compute_metrics=compute_metrics, 
    data_collator=collate_fn, 
    callbacks = [early_stop])

  train_results = trainer.train()

  #Build our final hugging face pipeline
  classifier = pipeline(
    "image-classification", 
    model=trainer.state.best_model_checkpoint, 
    tokenizer = model_def, 
    device_map='auto')
  
  #log the model to MLFlow
  #    pip_requirements is optional, buit it is used to specify a custom set of dependencies
  reqs = mlflow.transformers.get_default_pip_requirements(model)

  #    signature is used to specify the input and output schema.  Make a single prediction to get the output schema
  transform = ToPILImage()
  img = transform(val_ds[0]['image'])
  prediction = classifier(img)
  signature = infer_signature(
    model_input=np.array(img), 
    model_output=pd.DataFrame(prediction))
  
   #    log the model, set tags, and log metrics
  mlflow.transformers.log_model(
    artifact_path="model", 
    transformers_model=classifier, 
    pip_requirements=reqs,
    signature=signature)
  
  mlflow.set_tag("dbdemos", "pcb_classification")
  mlflow.log_metrics(train_results.metrics)

  #    Log the input dataset for lineage tracking from table to model
  src_dataset = mlflow.data.load_delta(
    table_name=f'{catalog}.{db}.training_dataset_augmented')
  mlflow.log_input(src_dataset, context="Training-Input")

# COMMAND ----------

# DBTITLE 1,Let's try our model to make sure it works as expected
import json
import io
from PIL import Image

def test_image(test, index):
  img = Image.open(io.BytesIO(test.iloc[index]['content']))
  print("Filename: " + test.iloc[index]['filename'])
  print("Ground truth label: " + test.iloc[index]['label'])
  print(f"predictions: {json.dumps(classifier(img), indent=4)}")
  display(img)

# Sample some images from the training dataset labeled as 'normal' and some labeled as 'damaged'
normal_samples = spark.read.table("training_dataset_augmented").filter("label == 'normal'").select("content", "filename", "label").limit(10).toPandas()
damaged_samples = spark.read.table("training_dataset_augmented").filter("label == 'damaged'").select("content", "filename", "label").limit(10).toPandas()

# Test the model using the first image from each group
test_image(normal_samples, 0)
print('\n\n=======================')
test_image(damaged_samples, 0)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Model deployment
# MAGIC
# MAGIC Our model is now trained. All we have to do is save it in our Model Registry and move it as Production ready. <br/>
# MAGIC For this demo we'll use our lastes run, but we could also search the best run with ` mlflow.search_runs` (based on the metric we defined during training).

# COMMAND ----------

# DBTITLE 1,Save the model in the registry & mark it for Production
# Register models in Unity Catalog
mlflow.set_registry_uri("databricks-uc")
MODEL_NAME = f"{catalog}.{db}.dbdemos_pcb_classification"

model_registered = mlflow.register_model("runs:/"+run.info.run_id+"/model", MODEL_NAME)
print("registering model version "+model_registered.version+" as production model")

## Alias the model version as the Production version
client = mlflow.tracking.MlflowClient()
client.set_registered_model_alias(
  name = MODEL_NAME, 
  version = model_registered.version,
  alias = "Production")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The model registry
# MAGIC Let's check out the model in the Unity Catalog model registry.
# MAGIC 1.  Open the Catalog Explorer from the left navigation menu
# MAGIC
# MAGIC <img src="https://github.com/databricks-demos/dbdemos-resources/blob/main/images/product/computer-vision/deeplearning-cv-pcb-model-lineage-01.png?raw=true"/>
# MAGIC
# MAGIC 2.  Use the search box or the Catalog browser to locate the `dbdemos_pcb_classification` model in your catalog and schema.
# MAGIC
# MAGIC <img src="https://github.com/databricks-demos/dbdemos-resources/blob/main/images/product/computer-vision/deeplearning-cv-pcb-model-lineage-02.png?raw=true"/>
# MAGIC
# MAGIC 3.  Open the version with the alias `@production`.
# MAGIC
# MAGIC <img src="https://github.com/databricks-demos/dbdemos-resources/blob/main/images/product/computer-vision/deeplearning-cv-pcb-model-lineage-03.png?raw=true"/>
# MAGIC
# MAGIC 4.  Select the Lineage tab.  Note that the `training_dataset_augmented` table is identified as an upstream connection to the model.
# MAGIC
# MAGIC <img src="https://github.com/databricks-demos/dbdemos-resources/blob/main/images/product/computer-vision/deeplearning-cv-pcb-model-lineage-04.png?raw=true"/>
# MAGIC
# MAGIC 5.  Click `See lineage graph`.  
# MAGIC
# MAGIC <img src="https://github.com/databricks-demos/dbdemos-resources/blob/main/images/product/computer-vision/deeplearning-cv-pcb-model-lineage-05.png?raw=true"/>
# MAGIC
# MAGIC 6.  Use the expansion icons and column names to explore the lineage of the model all the way back to the raw training data ingested from the Volume.
# MAGIC
# MAGIC <img src="https://github.com/databricks-demos/dbdemos-resources/blob/main/images/product/computer-vision/deeplearning-cv-pcb-model-lineage.png?raw=true"/>
# MAGIC
# MAGIC Unity Catalog Lineage provides end-to-end visibility into how data flows and is consumed in your organization from raw ingestion all the way to model training.  Lineage data is available through [System Tables](https://docs.databricks.com/en/admin/system-tables/lineage.html) as well as the UI.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next: Inference in batch and real-time 
# MAGIC
# MAGIC Our model is now trained and registered in MLflow Model Registry. Databricks mitigates the need for a lot of the anciliary code to train a model, so that you can focus on improving your model performance.
# MAGIC
# MAGIC The next step is now to use this model for inference - in batch or real-time behind a REST endpoint.
# MAGIC
# MAGIC Open the next [03-running-cv-inferences notebook]($./03-running-cv-inferences) to see how to leverage Databricks serving capabilities.
