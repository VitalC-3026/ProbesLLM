'''
This script trains probes on labeled data from csv files that
already have a column with embeddings and (optionally) saves the
best models to a directory. 

It is based on Amos Azaria's and Tom Mitchell's implementation for their paper `The Internal State of an LLM Knows When It's Lying.'
https://arxiv.org/abs/2304.13734

A number of parameters are specified in a configuration file, but 
you can also use command line arguments to override. 

Model options for OPT include: '6.7b', '2.7b', '1.3b', '350m'.

If you have generated embeddings for LLaMA, then options include: '7B', '13B', '30B', and '65B'.

5/20/23
'''

import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_curve, auc
from sklearn.metrics import confusion_matrix, accuracy_score
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import Dense
from pathlib import Path
import json
import logging
import os
from copy import deepcopy
import sys
import argparse


class MLP(nn.Module):
    def __init__(self, input_dim, hidden_dim=[256, 128, 64]) -> None:
        super(MLP, self).__init__()
        self.layers = nn.ModuleList()
        for i, dim in enumerate(hidden_dim):
            if i == 0:
                self.layers.append(nn.Linear(input_dim, dim))
            else:
                self.layers.append(nn.Linear(hidden_dim[i-1], dim))
            self.layers.append(nn.ReLU())
        self.layers.append(nn.Linear(hidden_dim[-1], 1))
        self.layers.append(nn.Sigmoid())
    
    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = layer(x)
        return x

def load_config(config_file):
    """
    Load the configuration settings from a JSON file.

    The function reads the JSON file and returns a dictionary of the configuration settings.

    Args:
        config_file (str): The path to the JSON configuration file.

    Returns:
        dict: The configuration settings.

    Raises:
        SystemExit: If the configuration file is not found or if there is an error parsing the JSON file.

    """
    try:
        with open(config_file) as json_file:
            return json.load(json_file)
    except FileNotFoundError:
        print(f"Config file {config_file} not found.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error parsing JSON in {config_file}.")
        sys.exit(1)

def load_datasets(dataset_names, layers_to_process, should_remove_period, input_path, model_name, idx):
    datasets = []
    dataset_paths = []
    for dataset_name in dataset_names:
        try:
            if should_remove_period:
                path = input_path / f"embeddings_{dataset_name}{model_name}_{abs(layers_to_process[idx])}_rmv_period.csv"
            else:
                path = input_path / f"embeddings_{dataset_name}{model_name}_{abs(layers_to_process[idx])}.csv"
            datasets.append(pd.read_csv(path))
            dataset_paths.append(path)
        except FileNotFoundError:
            print(f"File not found: {path}. Please ensure the dataset file exists.")
            sys.exit(1)
        except pd.errors.ParserError:
            print(f"Error parsing CSV file: {path}. Please ensure the file is in correct CSV format.")
            sys.exit(1)
    return datasets, dataset_paths

def prepare_datasets(datasets, dataset_names, test_first_only):
    """
    Prepares train and test datasets from a list of datasets.

    Arguments:
    datasets -- A list of pandas dataframes, where each dataframe is a separate dataset.
    dataset_names -- A list of strings, where each string is the name of a dataset corresponding to the 'datasets' list.
    test_first_only -- A boolean. If true, only the first dataset in 'datasets' is used as the test set, 
                          and the rest are used for training. If false, each dataset is used as the test set one-by-one, 
                          while the rest are used for training.

    Raises:
    ValueError -- If 'datasets' or 'dataset_names' is empty, or if they do not have the same length.
    Returns:
    train_datasets -- A list of pandas dataframes, where each dataframe is a training dataset.
    test_datasets -- A list of pandas dataframes, where each dataframe is a testing dataset.
    """

    if not datasets or not dataset_names:
        raise ValueError("Both 'datasets' and 'dataset_names' must be nonempty.")
    if len(datasets) != len(dataset_names):
        raise ValueError("'datasets' and 'dataset_names' must have the same length.")
    train_datasets = []
    test_datasets = []
    dataset_loop_length = 1 if test_first_only else len(dataset_names)
    for ds in range(dataset_loop_length):
        if test_first_only:
            test_df = datasets[0]
            dfs_to_concatenate = datasets[1:]  
        else:
            test_df = datasets[ds]
            dfs_to_concatenate = datasets[:ds] + datasets[ds + 1:]
        train_df = pd.concat(dfs_to_concatenate, ignore_index=True)
        train_datasets.append(train_df)
        test_datasets.append(test_df)
    return train_datasets, test_datasets

def correct_str(str_arr):
    """
    Converts a string representation of a numpy array into a comma-separated string.

    Arguments:
    str_arr -- A string representation of a numpy array.

    Returns:
    val_to_ret -- A comma-separated string derived from 'str_arr'.

    Note:
    This function assumes that 'str_arr' is a string representation of a numpy array 
    with dtype=float32. It removes the array formatting as well as whitespace and 
    newlines, and it also replaces '],' with ']'.
    """
    val_to_ret = (str_arr.replace("[array(", "")
                        .replace("dtype=float32)]", "")
                        .replace("\n","")
                        .replace(" ","")
                        .replace("],","]")
                        .replace("[","")
                        .replace("]",""))
    return val_to_ret
        

def define_model(input_dim):
    """
    Defines and compiles a Sequential model in Keras.

    Arguments:
    input_dim -- The dimension of the input data (positive integer).

    Returns:
    model -- A compiled Sequential model.

    This function creates a Sequential model with three hidden layers, each followed 
    by a ReLU activation function. The output layer uses a sigmoid activation function.
    The model is compiled with the Adam optimizer, binary cross-entropy loss, and 
    accuracy as a metric.
    
    Raises:
    ValueError -- If input_dim is not a positive integer.
    """
    if not isinstance(input_dim, int) or input_dim <= 0:
        raise ValueError("Input dimension must be a positive integer.")
    model = MLP(input_dim=input_dim, hidden_dim=[256, 128, 64])
    return model


def get_loss(preds, labels, add_loss=False):
    loss = F.binary_cross_entropy(preds.reshape(-1), labels).reshape(1)
    pos_idx = torch.where(labels == 1)[0]
    neg_idx = torch.where(labels == 0)[0]
    pos_preds = preds[pos_idx]
    neg_preds = preds[neg_idx]
    if add_loss:
        consistent_loss = ((pos_preds - (1 - neg_preds)) ** 2).mean(0)
        informative_loss = (torch.min(pos_preds, neg_preds)**2).mean(0)
        loss += consistent_loss + informative_loss
    return loss


def train_model(model, pos_embeddings, neg_embeddings, optimizer, epochs=100, batch_size=32, add_loss=False):
    """
    Trains the input model on the provided embeddings and labels.
    
    Parameters:
    model (torch Model, nn.Module): The model to be trained.
    pos_embeddings (numpy.ndarray): The embeddings used for training. Each embedding should correspond to label 1.
    neg_embeddings (numpy.ndarray): The embeddings used for training. Each embedding should correspond to label 0.
    train_labels (Series or ndarray): The labels for each training embedding.

    Returns:
    model (torch Model, nn.Module): The trained model.

    Raises:
    ValueError: If the length of 'train_embeddings' and 'train_labels' does not match.
    """
    if len(pos_embeddings) != len(neg_embeddings):
        raise ValueError("Training positive and negative embeddings must have the same length.")
    model.train()
    bz = batch_size // 2
    for ep in range(epochs):
        ep_loss = 0
        batch_num = 0
        for i in range(0, len(pos_embeddings), bz):
            pos_data = torch.tensor(pos_embeddings[i:i+bz].astype(np.float32))
            neg_data = torch.tensor(neg_embeddings[i:i+bz].astype(np.float32))
            data = torch.concat((pos_data, neg_data), dim=0)
            labels = np.array([1] * pos_data.shape[0] + [0] * neg_data.shape[0])
            labels = torch.tensor(labels.astype(np.float32))
            indices = torch.randperm(data.size(0))
            shuffled_data = data[indices]
            shuffled_labels = labels[indices]
            optimizer.zero_grad()
            outputs = model(data)
            loss = get_loss(outputs, labels, add_loss)
            loss.backward()
            optimizer.step()
            batch_num += 1
            ep_loss = loss.item()
        print(f"Epoch {ep}/{epochs}: Training Loss: {ep_loss / batch_num}")
    return model

def evaluate_model(model, test_embeddings, test_labels=None, threshold=0.5, batch_size=32):
    """
    Evaluates the performance of the trained model on the test data.

    Parameters:
    model (keras Model): The trained model to be evaluated.
    test_embeddings (numpy.ndarray): The embeddings used for testing. Each embedding should correspond to a label in 'test_labels'.
    test_labels (Series or ndarray): The labels for each test embedding.

    Returns:
    loss (float): The loss value calculated by the model on the test data.
    accuracy (float): The accuracy of the model on the test data, as a decimal.

    Raises:
    ValueError: If the length of 'test_embeddings' and 'test_labels' does not match.
    """
    if test_labels is not None and len(test_embeddings) != len(test_labels):
        raise ValueError("Test embeddings and labels must have the same length.")
    test_loss, batch_num, test_acc = 0, 0, 0
    predictions = []
    model.eval()
    if test_labels is not None:
        with torch.no_grad():
            for i in range(0, len(test_embeddings), batch_size):
                data = torch.tensor(test_embeddings[i:i+batch_size].astype(np.float32))
                labels = torch.tensor(test_labels[i:i+batch_size].astype(np.float32))
                outputs = model(data)
                predictions.append(outputs)
                loss = get_loss(outputs, labels)
                test_loss += loss.item()
                acc = torch.sum((outputs > threshold) == labels)
                test_acc += acc
                batch_num += 1
        return torch.cat(predictions, dim=0), loss / batch_num, test_acc / batch_num
    else:
        with torch.no_grad():
            for i in range(0, len(test_embeddings), batch_size):
                data = torch.tensor(test_embeddings[i:i+batch_size].astype(np.float32))
                outputs = model(data)
                predictions.append(outputs)
        return torch.cat(predictions, dim=0), 0, 0

def compute_roc_curve(test_labels, test_pred_prob):
    """
    Computes the Receiver Operating Characteristic (ROC) curve and the area under the curve (AUC).

    Parameters:
    test_labels (Series or ndarray): The true labels for the test data.
    test_pred_prob (numpy.ndarray): The predicted probabilities for each data point in the test set.

    Returns:
    roc_auc (float): The area under the ROC curve, a single scalar value representing the model's overall performance.
    fpr (numpy.ndarray): The false positive rate at various decision thresholds.
    tpr (numpy.ndarray): The true positive rate at various decision thresholds.

    Note:
    This function assumes a binary classification task.
    """
    fpr, tpr, _ = roc_curve(test_labels, test_pred_prob)  # Assuming binary classification
    roc_auc = auc(fpr, tpr)
    return roc_auc, fpr, tpr

def find_optimal_threshold(X, y, model):
    """
    Finds the optimal threshold for a binary classification model by maximizing the accuracy score over the ROC curve thresholds.

    Parameters:
    X (numpy.ndarray): Input feature array.
    y (numpy.ndarray or list): True binary labels in range {0, 1} or {-1, 1}. If labels are not binary, pos_label should be explicitly given.
    model (keras.Model): The binary classification model.

    Returns:
    float: The optimal threshold value.

    Raises:
    ValueError: If the dimensions of X and y do not match.
    """
    # Predict probabilities for the data set
    y_pred_prob, _, _ = evaluate_model(model, X)

    # Compute ROC curve to find the optimal threshold
    fpr_val, tpr_val, thresholds_val = roc_curve(y, y_pred_prob)
    optimal_threshold = thresholds_val[np.argmax([accuracy_score(y, y_pred_prob > thr) for thr in thresholds_val])]

    return optimal_threshold


def print_results(results, dataset_names, repeat_each, layer_num_from_end):
    """
    Prints the average accuracy, AUC, and optimal threshold for each dataset, and returns a list of these results.

    Parameters:
    results (list of tuples): Each tuple represents the results for a dataset and contains the dataset name, index, 
                              accuracy, AUC, optimal threshold, and test accuracy.
    dataset_names (list of str): The names of the datasets.
    repeat_each (int): The number of times each experiment is repeated.
    layer_num_from_end (int): The index of the layer from the end of the model.

    Returns:
    list of str: Each string contains the average results for a dataset.
    Raises:
    ValueError: If the length of the results list is not equal to the length of the dataset_names list multiplied by repeat_each.
    """
    if len(results) != len(dataset_names) * repeat_each:
        raise ValueError("Results array length should be equal to dataset_names length multiplied by repeat_each.")
    overall_res = []
    for ds in range(len(dataset_names)):
        relevant_results_portion = results[repeat_each*ds:repeat_each*(ds+1)]
        acc_list = [t[2] for t in relevant_results_portion]
        auc_list = [t[3] for t in relevant_results_portion]
        opt_thresh_list = [t[4] for t in relevant_results_portion]
        avg_acc = sum(acc_list) / len(acc_list)
        avg_auc = sum(auc_list) / len(auc_list)
        avg_thrsh = sum(opt_thresh_list) / len(opt_thresh_list)
        text_res = ("dataset: " + str(dataset_names[ds]) + " layer_num_from_end:" 
                    + str(layer_num_from_end) + " Avg_acc:" + str(avg_acc) 
                    + " Avg_AUC:" + str(avg_auc) + " Avg_threshold:" 
                    + str(avg_thrsh))
        print(text_res)
        overall_res.append(text_res)

    return overall_res

def main():
    # Define the logger
    try:
        logging.basicConfig(filename='classification.log', level=logging.INFO, 
                            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        logger = logging.getLogger(__name__)
    except Exception as e:
        print(f"Error setting up logging: {e}")
        return
    
    logger.info("Execution started.")

    # Load the config
    try:
        config_parameters = load_config("config.json")
    except Exception as e:
        logger.error(f"Error loading config file: {e}")
        return

    parser = argparse.ArgumentParser(description="Train probes on processed and labeled datasets.")
    parser.add_argument("--model", 
                        help="Name of the language model to use: '6.7b', '2.7b', '1.3b', '350m'")
    parser.add_argument("--layers", nargs='*', 
                        help="List of layers of the LM to save embeddings from indexed negatively from the end")
    parser.add_argument("--dataset_names", nargs='*',
                        help="List of dataset names without csv extension.")
    parser.add_argument("--remove_period", type=bool, help="True if you want to extract embedding for last token before the final period.")
    parser.add_argument("--test_first_only", type=bool, help="True if you only want to use the first dataset for testing a probe.")
    parser.add_argument("--save_probes", type=bool, help="True if you want to save the trained probes.")
    parser.add_argument("--repeat_each", type=int, help="How many times to train a randomly initialized probe for each dataset.")
    parser.add_argument("--add_new_loss", type=bool, default=False, help="True if we add new unsupervised loss")
    parser.add_argument("--epochs", type=int, default=100, help="Epochs to train the models")
    parser.add_argument("--batch_size", type=int, default=32, help="batch size for training and evaluation")
    parser.add_argument("--lr", type=float, default=1e-3, help="learning rate")
    args = parser.parse_args()

    model_name = args.model if args.model is not None else config_parameters["model"]
    should_remove_period = args.remove_period if args.remove_period is not None else config_parameters["remove_period"]
    layers_to_process = [int(x) for x in args.layers] if args.layers is not None else config_parameters["layers_to_use"]
    dataset_names = args.dataset_names if args.dataset_names is not None else config_parameters["list_of_datasets"]
    test_first_only = args.test_first_only if args.test_first_only is not None else config_parameters["test_first_only"]
    save_probes = args.save_probes if args.save_probes is not None else config_parameters["save_probes"]
    repeat_each = args.repeat_each if args.repeat_each is not None else config_parameters["repeat_each"]
    learning_rate = args.lr if args.lr is not None else config_parameters["learning_rate"]
    batch_size = args.batch_size if args.batch_size is not None else config_parameters["batch_size"]
    input_path = Path(config_parameters["processed_dataset_path"])
    probes_path = Path(config_parameters["probes_dir"])
    
    # Iterate over the layers in "layer_num_list"
    for idx in range(len(layers_to_process)):
        # Load the datasets
        datasets, dataset_paths = load_datasets(dataset_names, layers_to_process, should_remove_period, input_path, model_name, idx)

        # Prepare the datasets
        train_datasets, test_datasets = prepare_datasets(datasets, dataset_names, test_first_only)

        overall_res = []
        results = []
 
        # Iterate over each test dataset
        for count, (test_dataset, train_dataset, test_dataset_path) in enumerate(zip(test_datasets, train_datasets, dataset_paths)):

            # Split the dataset into embeddings and labels
            train_embeddings = np.array([np.fromstring(correct_str(embedding), sep=',') for embedding in train_dataset['embeddings'].tolist()])
            train_labels = train_dataset['label'].to_numpy()
            pos_embeddings = train_embeddings[np.where(train_labels == 1)[0]]
            neg_embeddings = train_embeddings[np.where(train_labels == 0)[0]]
            min_len = min(pos_embeddings.shape[0], neg_embeddings.shape[0])
            if pos_embeddings.shape[0] > min_len:
                indices = np.random.choice(pos_embeddings.shape[0], min_len, replace=False)
                pos_embeddings = pos_embeddings[indices]
            if neg_embeddings.shape[0] > min_len:
                indices = np.random.choice(neg_embeddings.shape[0], min_len, replace=False)
                neg_embeddings = neg_embeddings[indices]
            test_embeddings = np.array([np.fromstring(correct_str(embedding), sep=',') for embedding in test_dataset['embeddings'].tolist()])
            test_labels = test_dataset['label'].to_numpy()
            # Repeat training and testing for specified number of times
            best_accuracy = 0
            best_model = None
            all_probs_list = []
            for i in range(repeat_each):

                # Define the model
                model = define_model(train_embeddings.shape[1])
                # Define optimizer
                optimizer = torch.optim.Adam(model.parameters(),
                                             lr=learning_rate,
                                             weight_decay=1e-4)
            
                # Train the model on full training data
                model = train_model(model, pos_embeddings, neg_embeddings, optimizer, 
                                    epochs=args.epochs, batch_size=batch_size, add_loss=args.add_new_loss)

                # Find the optimal threshold and compute validation set accuracy
                optimal_threshold = find_optimal_threshold(train_embeddings, train_labels, model)

                # Evaluate the model
                test_pred_prob, loss, accuracy = evaluate_model(model, test_embeddings, test_labels, threshold=optimal_threshold, batch_size=batch_size)

                if accuracy > best_accuracy:
                    best_accuracy = accuracy
                    best_model = model

                all_probs_list.append(deepcopy(test_pred_prob)) #Store probabilities

                # Compute ROC curve and ROC area
                roc_auc, fpr, tpr = compute_roc_curve(test_labels, test_pred_prob)

                # Compute test set accuracy using the optimal threshold
                test_accuracy = accuracy_score(test_labels, test_pred_prob > optimal_threshold)

                results.append((dataset_names[count], i, accuracy, roc_auc, optimal_threshold, test_accuracy))

            # Save the best model
            if save_probes:
                try:
                    if not os.path.exists(probes_path):
                        os.makedirs(probes_path)
                    if should_remove_period:
                        model_path = os.path.join(probes_path, f"{model_name}_{abs(layers_to_process[idx])}_{dataset_names[count]}_rp.h5")
                    else:
                        model_path = os.path.join(probes_path, f"{model_name}_{abs(layers_to_process[idx])}_{dataset_names[count]}.h5")
                    best_model.save(model_path)
                    torch.save()
                except Exception as e:
                    logger.error(f"Error saving the model: {e}")

            #Add probabilities to the CSV file
            test_dataset_copy = test_dataset.copy() #Make a copy of the dataset
            

            test_dataset_copy['average_probability'] = np.mean(all_probs_list, axis=0)
            for i, prob in enumerate(all_probs_list):
                test_dataset_copy[f'model_{i}_probability'] = prob
            # Round off the probabilities to the first four digits
            for col in test_dataset_copy.columns:
                if 'probability' in col:
                    test_dataset_copy[col] = test_dataset_copy[col].round(4)

            #Define the new filename for the copy
            original_path = Path(test_dataset_path)
            new_path = original_path.with_name(original_path.name.replace('.csv', '_predictions.csv'))

            # Save the modified copy as a new CSV file
            test_dataset_copy.to_csv(new_path, index=False) 
        # Print the results
        if not test_first_only:
            avg_res = print_results(results, dataset_names, repeat_each, layers_to_process[idx])
            overall_res.extend(avg_res)

        
    logger.info("Execution completed.")
    logger.info("Overall results: " + str(overall_res))

if __name__ == "__main__":
    main()
 
                                                

