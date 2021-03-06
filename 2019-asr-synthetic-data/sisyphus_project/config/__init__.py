from sisyphus import *
import copy

Path = setup_path(__package__)

from recipe.corpus.librispeech import LibriSpeechToBliss
from recipe.corpus import BlissToZipDataset
from recipe.text.bliss import BlissExtractRawText
from recipe.text.subword_units import CreateSubwordsAndVocab

from recipe.default_values import FFMPEG_BINARY

def prepare_data():

  dataset_names = ['dev-clean', 'dev-other', 'test-clean', 'test-other',
                   'train-clean-100', 'train-clean-360']

  bliss_flac_corpus_dict = {}
  zip_flac_corpus_dict = {}

  for dataset_name in dataset_names:
    dataset_path = Path("../data/dataset-raw/LibriSpeech/%s/" % dataset_name)

    ls_to_bliss_job = LibriSpeechToBliss(corpus_path=dataset_path, name=dataset_name)
    ls_to_bliss_job.add_alias("data/LibriSpeechToBliss/%s" % dataset_name)
    bliss_flac_corpus_dict[dataset_name] = ls_to_bliss_job.out
    tk.register_output("data/bliss/%s.xml.gz" % dataset_name, ls_to_bliss_job.out)

    bliss_to_zip_job = BlissToZipDataset(name=dataset_name, corpus_file=ls_to_bliss_job.out, use_full_seq_name=False)
    bliss_to_zip_job.add_alias("data/BlissToZipDataset/%s" % dataset_name)
    zip_flac_corpus_dict[dataset_name] = bliss_to_zip_job.out
    tk.register_output("data/asr_zip/%s.zip" % dataset_name, bliss_to_zip_job.out)

  return bliss_flac_corpus_dict, zip_flac_corpus_dict


def build_subwords(bliss_corpora):
  """

  :param list bliss_corpora:
  :return:
  """
  corpus_texts = []
  for bliss_corpus in bliss_corpora:
    extract_text_job = BlissExtractRawText(bliss_corpus)
    corpus_texts.append(extract_text_job.out)

  from recipe.text import Concatenate
  text = Concatenate(corpus_texts).out
  subwords_job = CreateSubwordsAndVocab(text=text, num_segments=10000)
  subwords_job.add_alias("data/subwords/CreateSubwordsAndVocab")

  bpe_codes = subwords_job.out_bpe
  bpe_vocab = subwords_job.out_vocab
  bpe_vocab_size = subwords_job.out_vocab_size

  tk.register_output("data/subwords/bpe.codes", bpe_codes)
  tk.register_output("data/subwords/bpe.vocab", bpe_vocab)
  tk.register_output("data/subwords/bpe.vocab_size", bpe_vocab_size)

  return bpe_codes, bpe_vocab, bpe_vocab_size


def get_asr_dataset_stats(zip_dataset):

  config = {'train':
              {'class': 'OggZipDataset',
               'audio': {},
               'targets': None,
               'path': zip_dataset}
           }

  from recipe.returnn.dataset import ExtractDatasetStats
  dataset_stats_job = ExtractDatasetStats(config)
  dataset_stats_job.add_alias("data/stats/ExtractDatasetStats")

  mean = dataset_stats_job.mean_file
  std_dev = dataset_stats_job.std_dev_file

  tk.register_output('data/stats/norm.mean.txt', mean)
  tk.register_output('data/stats/norm.std_dev.txt', std_dev)

  return mean, std_dev


def train_asr_config(config, name, parameter_dict=None):
  from recipe.returnn import RETURNNTrainingFromFile
  asr_train = RETURNNTrainingFromFile(config, parameter_dict=parameter_dict, mem_rqmt=16)
  asr_train.add_alias("asr_training/" + name)

  # TODO: Remove
  asr_train.rqmt['qsub_args'] = '-l qname=%s' % "*080*"

  asr_train.rqmt['time'] = 167
  asr_train.rqmt['cpu'] = 8
  tk.register_output("asr_training/" + name + "_model", asr_train.model_dir)
  tk.register_output("asr_training/" + name + "_training-scores", asr_train.learning_rates)
  return asr_train

def main():
  bliss_dict, zip_dict = prepare_data()

  bpe_codes, bpe_vocab, num_classes = build_subwords([bliss_dict['train-clean-100'],
                                                      bliss_dict['train-clean-360']])

  mean, stddev = get_asr_dataset_stats(zip_dict['train-clean-100'])

  asr_global_parameter_dict = {
    'ext_norm_mean': mean,
    'ext_norm_std_dev': stddev,
    'ext_bpe_file': bpe_codes,
    'ext_vocab_file': bpe_vocab,
    'ext_num_classes': num_classes
  }

  initial_checkpoint_training_params = {
    'ext_partition_epoch': 20,
    'ext_training_zips': [zip_dict['train-clean-100']],
    'ext_dev_zips': [zip_dict['dev-clean'],
                     zip_dict['dev-other']],
    'ext_num_epochs': 80
  }

  baseline_training_params = copy.deepcopy(initial_checkpoint_training_params)
  baseline_training_params['ext_num_epochs'] = 170

  initial_checkpoint_training_params.update(asr_global_parameter_dict)

  asr_training_config = Path("returnn_configs/asr/train-clean-100.exp3.ctc.ogg.lrwarmupextra10.config")
  initial_training_job = train_asr_config(asr_training_config, "librispeech-100-initial-training",
                             initial_checkpoint_training_params)

  baseline_training_params['import_model_train_epoch1'] = initial_training_job.models[80].model
  baseline_training_params.update(asr_global_parameter_dict)
  continued_training_job = train_asr_config(asr_training_config, "librispeech-100-baseline-training",
                                            baseline_training_params)

  ##########################3
  # TTS

  tts_bliss_dict = {k:v for k,v in bliss_dict.items() if k in ['dev-clean', 'train-clean-100']}
  tts_bliss_corpora, tts_zip_corpora, char_vocab = prepare_tts_data(tts_bliss_dict)

  mean, stddev = get_tts_dataset_stats(tts_zip_corpora['tts-train-clean-100'])

  tts_global_parameter_dict = {
    'ext_norm_mean_value': mean,
    'ext_norm_std_dev_value': stddev,
    'ext_char_vocab': char_vocab,
    'ext_training_zips': [tts_zip_corpora['tts-train-clean-100']],
    'ext_dev_zips': [tts_zip_corpora['tts-dev-clean']],
    'ext_num_epochs': 200,
    'ext_partition_epoch': 20,
  }

  tts_training_config = Path("returnn_configs/tts/tts-clean-100.dec640.enc256.enclstm512.config")
  tts_training_job = train_tts_config(tts_training_config, name="tts-baseline-training",
                                      parameter_dict=tts_global_parameter_dict)

def process_corpus(bliss_corpus, char_vocab, silence_duration, name=None):
  """
  process a bliss corpus to be suited for TTS training
  :param self:
  :param bliss_corpus:
  :param name:
  :return:
  """
  from recipe.text.bliss import ProcessBlissText
  ljs = ProcessBlissText(bliss_corpus, [('end_token',{'token': '~'})],
                         vocabulary=char_vocab)

  from recipe.corpus.ffmpeg import BlissFFMPEGJob, BlissRecoverDuration

  filter_string = '-af "silenceremove=stop_periods=-1:window=%f:stop_duration=0.01:stop_threshold=-40dB"' % \
                  silence_duration

  ljs_nosilence = BlissFFMPEGJob(ljs.out, filter_string, ffmpeg_binary=FFMPEG_BINARY, output_format="wav")
  ljs_nosilence.rqmt['time'] = 24

  ljs_nosilence_recover = BlissRecoverDuration(ljs_nosilence.out)

  return ljs_nosilence_recover.out

def prepare_tts_data(bliss_dict):
  """

  :param dict bliss_dict:
  :return:
  """

  from recipe.returnn.vocabulary import BuildCharacterVocabulary
  build_char_vocab_job = BuildCharacterVocabulary(uppercase=True)
  char_vocab = build_char_vocab_job.out

  processed_corpora = {}
  processed_zip_corpora = {}
  for name, corpus in bliss_dict.items():
    tts_name = "tts-" + name
    processed_corpus = process_corpus(bliss_corpus=corpus,
                                      char_vocab=char_vocab,
                                      silence_duration=0.1,
                                      name=tts_name)
    processed_corpora[tts_name] = processed_corpus
    tk.register_output("data/bliss/%s.xml.gz" % name, processed_corpus)

    processed_zip_corpora[tts_name] = BlissToZipDataset(tts_name, processed_corpus).out

  return processed_corpora, processed_zip_corpora, char_vocab

def get_tts_dataset_stats(zip_dataset):

  config = {'train':
              {'class': 'OggZipDataset',
               'audio': {'feature_options': {'fmin': 60},
                         'features': 'db_mel_filterbank',
                         'num_feature_filters': 80,
                         'peak_normalization': False,
                         'preemphasis': 0.97,
                         'step_len': 0.0125,
                         'window_len': 0.05},
               'targets': None,
               'path': zip_dataset}
            }

  from recipe.returnn.dataset import ExtractDatasetStats
  dataset_stats_job = ExtractDatasetStats(config)
  dataset_stats_job.add_alias("data/tts_stats/ExtractDatasetStats")

  mean = dataset_stats_job.mean
  std_dev = dataset_stats_job.std_dev

  tk.register_output('data/tts_stats/norm.mean.txt', mean)
  tk.register_output('data/tts_stats/norm.std_dev.txt', std_dev)

  return mean, std_dev

def train_tts_config(config, name, parameter_dict=None):
  from recipe.returnn import RETURNNTrainingFromFile
  asr_train = RETURNNTrainingFromFile(config, parameter_dict=parameter_dict, mem_rqmt=16)
  asr_train.add_alias("tts_training/" + name)

  # TODO: Remove
  asr_train.rqmt['qsub_args'] = '-l qname=%s' % "*080*"

  asr_train.rqmt['time'] = 167
  asr_train.rqmt['cpu'] = 8
  tk.register_output("tts_training/" + name + "_model", asr_train.model_dir)
  tk.register_output("tts_training/" + name + "_training-scores", asr_train.learning_rates)
  return asr_train

