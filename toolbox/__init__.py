import sys
import traceback
from pathlib import Path
from time import perf_counter as timer

import numpy as np
import torch

from codificador import inference as encoder
from synthesizer.inference import Synthesizer
from toolbox.ui import UI
from toolbox.utterance import Utterance
from vocoder import inference as vocoder


# Use este directorio para la estructura de datos , o modificarlo si es necesario 
recognized_datasets = [
    "LibriSpeech/dev-clean",
    "LibriSpeech/dev-other",
    "LibriSpeech/test-clean",
    "LibriSpeech/test-other",
    "LibriSpeech/train-clean-100",
    "LibriSpeech/train-clean-360",
    "LibriSpeech/train-other-500",
    "LibriTTS/dev-clean",
    "LibriTTS/dev-other",
    "LibriTTS/test-clean",
    "LibriTTS/test-other",
    "LibriTTS/train-clean-100",
    "LibriTTS/train-clean-360",
    "LibriTTS/train-other-500",
    "LJSpeech-1.1",
    "VoxCeleb1/wav",
    "VoxCeleb1/test_wav",
    "VoxCeleb2/dev/aac",
    "VoxCeleb2/test/aac",
    "VCTK-Corpus/wav48",
]

# Maximo de  wavs generadas para mantener en memoria 
MAX_WAVS = 15


class Toolbox:
    def __init__(self, datasets_root: Path, models_dir: Path, seed: int=None):
        sys.excepthook = self.excepthook
        self.datasets_root = datasets_root
        self.utterances = set()
        self.current_generated = (None, None, None, None) # speaker_name, spec, breaks, wav

        self.synthesizer = None # type: Synthesizer
        self.current_wav = None
        self.waves_list = []
        self.waves_count = 0
        self.waves_namelist = []

        # Compruebe webrtcvad (permite la eliminación de silencios en la salida del codificador de voz)
        try:
            import webrtcvad
            self.trim_silences = True
        except:
            self.trim_silences = False

        # Inicializando los eventos y la interfaz
        self.ui = UI()
        self.reset_ui(models_dir, seed)
        self.setup_events()
        self.ui.start()

    def excepthook(self, exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb)
        self.ui.log("Exception: %s" % exc_value)

    def setup_events(self):
        # Selección de conjunto de datos, oradora y enunciado
        self.ui.browser_load_button.clicked.connect(lambda: self.load_from_browser())
        random_func = lambda level: lambda: self.ui.populate_browser(self.datasets_root,
                                                                     recognized_datasets,
                                                                     level)
        self.ui.random_dataset_button.clicked.connect(random_func(0))
        self.ui.random_speaker_button.clicked.connect(random_func(1))
        self.ui.random_utterance_button.clicked.connect(random_func(2))
        self.ui.dataset_box.currentIndexChanged.connect(random_func(1))
        self.ui.speaker_box.currentIndexChanged.connect(random_func(2))

        # Model selection
        self.ui.encoder_box.currentIndexChanged.connect(self.init_encoder)
        def func():
            self.synthesizer = None
        self.ui.synthesizer_box.currentIndexChanged.connect(func)
        self.ui.vocoder_box.currentIndexChanged.connect(self.init_vocoder)

        # seleccion de modelo
        func = lambda: self.load_from_browser(self.ui.browse_file())
        self.ui.browser_browse_button.clicked.connect(func)
        func = lambda: self.ui.draw_utterance(self.ui.selected_utterance, "current")
        self.ui.utterance_history.currentIndexChanged.connect(func)
        func = lambda: self.ui.play(self.ui.selected_utterance.wav, Synthesizer.sample_rate)
        self.ui.play_button.clicked.connect(func)
        self.ui.stop_button.clicked.connect(self.ui.stop)
        self.ui.record_button.clicked.connect(self.record)

        #Audio
        self.ui.setup_audio_devices(Synthesizer.sample_rate)

        #Wav reproducir  & guardar
        func = lambda: self.replay_last_wav()
        self.ui.replay_wav_button.clicked.connect(func)
        func = lambda: self.export_current_wave()
        self.ui.export_wav_button.clicked.connect(func)
        self.ui.waves_cb.currentIndexChanged.connect(self.set_current_wav)

        # Generacion
        func = lambda: self.synthesize() or self.vocode()
        self.ui.generate_button.clicked.connect(func)
        self.ui.synthesize_button.clicked.connect(self.synthesize)
        self.ui.vocode_button.clicked.connect(self.vocode)
        self.ui.random_seed_checkbox.clicked.connect(self.update_seed_textbox)

        # UMAP legend
        self.ui.clear_button.clicked.connect(self.clear_utterances)

    def set_current_wav(self, index):
        self.current_wav = self.waves_list[index]

    def export_current_wave(self):
        self.ui.save_audio_file(self.current_wav, Synthesizer.sample_rate)

    def replay_last_wav(self):
        self.ui.play(self.current_wav, Synthesizer.sample_rate)

    def reset_ui(self, models_dir: Path, seed: int=None):
        self.ui.populate_browser(self.datasets_root, recognized_datasets, 0, True)
        self.ui.populate_models(models_dir)
        self.ui.populate_gen_options(seed, self.trim_silences)

    def load_from_browser(self, fpath=None):
        if fpath is None:
            fpath = Path(self.datasets_root,
                         self.ui.current_dataset_name,
                         self.ui.current_speaker_name,
                         self.ui.current_utterance_name)
            name = str(fpath.relative_to(self.datasets_root))
            speaker_name = self.ui.current_dataset_name + '_' + self.ui.current_speaker_name

            # Seleccione la siguiente expresión
            if self.ui.auto_next_checkbox.isChecked():
                self.ui.browser_select_next()
        elif fpath == "":
            return
        else:
            name = fpath.name
            speaker_name = fpath.parent.name

         # Obtenga el wav del disco. Tomamos el wav con el formato vocoder/synthesizer para
         # reproducción, para tener una comparación justa con el audio generado
        wav = Synthesizer.load_preprocess_wav(fpath)
        self.ui.log("Loaded %s" % name)

        self.add_real_utterance(wav, name, speaker_name)

    def record(self):
        wav = self.ui.record_one(encoder.sampling_rate, 5)
        if wav is None:
            return
        self.ui.play(wav, encoder.sampling_rate)

        speaker_name = "user01"
        name = speaker_name + "_rec_%05d" % np.random.randint(100000)
        self.add_real_utterance(wav, name, speaker_name)

    def add_real_utterance(self, wav, name, speaker_name):
        # calcule el espectograma  de Mel
        spec = Synthesizer.make_spectrogram(wav)
        self.ui.draw_spec(spec, "current")

        # Calcular la incrustación
        if not encoder.is_loaded():
            self.init_encoder()
        encoder_wav = encoder.preprocess_wav(wav)
        embed, partial_embeds, _ = encoder.embed_utterance(encoder_wav, return_partials=True)

        # Añadir la expresion 
        utterance = Utterance(name, speaker_name, wav, spec, embed, partial_embeds, False)
        self.utterances.add(utterance)
        self.ui.register_utterance(utterance)

        # plotearlo 
        self.ui.draw_embed(embed, name, "current")
        self.ui.draw_umap_projections(self.utterances)

    def clear_utterances(self):
        self.utterances.clear()
        self.ui.draw_umap_projections(self.utterances)

    def synthesize(self):
        self.ui.log("Generating the mel spectrogram...")
        self.ui.set_loading(1)

        # Actualizar la semilla aleatoria del sintetizador
        if self.ui.random_seed_checkbox.isChecked():
            seed = int(self.ui.seed_textbox.text())
            self.ui.populate_gen_options(seed, self.trim_silences)
        else:
            seed = None

        if seed is not None:
            torch.manual_seed(seed)

        # Sintetizar el espectrograma
        if self.synthesizer is None or seed is not None:
            self.init_synthesizer()

        texts = self.ui.text_prompt.toPlainText().split("\n")
        embed = self.ui.selected_utterance.embed
        embeds = [embed] * len(texts)
        specs = self.synthesizer.synthesize_spectrograms(texts, embeds)
        breaks = [spec.shape[1] for spec in specs]
        spec = np.concatenate(specs, axis=1)

        self.ui.draw_spec(spec, "generated")
        self.current_generated = (self.ui.selected_utterance.speaker_name, spec, breaks, None)
        self.ui.set_loading(0)

    def vocode(self):
        speaker_name, spec, breaks, _ = self.current_generated
        assert spec is not None

        # Inicialice el modelo de vocoder,  si el usuario proporciona una semilla
        if self.ui.random_seed_checkbox.isChecked():
            seed = int(self.ui.seed_textbox.text())
            self.ui.populate_gen_options(seed, self.trim_silences)
        else:
            seed = None

        if seed is not None:
            torch.manual_seed(seed)

        # Sintetizar la forma de onda
        if not vocoder.is_loaded() or seed is not None:
            self.init_vocoder()

        def vocoder_progress(i, seq_len, b_size, gen_rate):
            real_time_factor = (gen_rate / Synthesizer.sample_rate) * 1000
            line = "Waveform generation: %d/%d (batch size: %d, rate: %.1fkHz - %.2fx real time)" \
                   % (i * b_size, seq_len * b_size, b_size, gen_rate, real_time_factor)
            self.ui.log(line, "overwrite")
            self.ui.set_loading(i, seq_len)
        if self.ui.current_vocoder_fpath is not None:
            self.ui.log("")
            wav = vocoder.infer_waveform(spec, progress_callback=vocoder_progress)
        else:
            self.ui.log("Waveform generation with Griffin-Lim... ")
            wav = Synthesizer.griffin_lim(spec)
        self.ui.set_loading(0)
        self.ui.log(" Done!", "append")

        # Agregar descansos
        b_ends = np.cumsum(np.array(breaks) * Synthesizer.hparams.hop_size)
        b_starts = np.concatenate(([0], b_ends[:-1]))
        wavs = [wav[start:end] for start, end, in zip(b_starts, b_ends)]
        breaks = [np.zeros(int(0.15 * Synthesizer.sample_rate))] * len(breaks)
        wav = np.concatenate([i for w, b in zip(wavs, breaks) for i in (w, b)])

        # Recortar los silencios excesivos
        if self.ui.trim_silences_checkbox.isChecked():
            wav = encoder.preprocess_wav(wav)

        # Play it
        wav = wav / np.abs(wav).max() * 0.97
        self.ui.play(wav, Synthesizer.sample_rate)

        # Nómbrelo (el historial se muestra en el cuadro combinado)
         # TODO ¿mejor nombre para los elementos del cuadro combinado?
        wav_name = str(self.waves_count + 1)

        # Cuadro combinado actualiza las olas
        self.waves_count += 1
        if self.waves_count > MAX_WAVS:
          self.waves_list.pop()
          self.waves_namelist.pop()
        self.waves_list.insert(0, wav)
        self.waves_namelist.insert(0, wav_name)

        self.ui.waves_cb.disconnect()
        self.ui.waves_cb_model.setStringList(self.waves_namelist)
        self.ui.waves_cb.setCurrentIndex(0)
        self.ui.waves_cb.currentIndexChanged.connect(self.set_current_wav)

        # actializar actual swav
        self.set_current_wav(0)

        # Habilitar los botones de reproducción y guardar:
        self.ui.replay_wav_button.setDisabled(False)
        self.ui.export_wav_button.setDisabled(False)

       # Calcular la incrustación
         # TODO: esto es problemático con diferentes tasas de muestreo, tengo que arreglarlo
        if not encoder.is_loaded():
            self.init_encoder()
        encoder_wav = encoder.preprocess_wav(wav)
        embed, partial_embeds, _ = encoder.embed_utterance(encoder_wav, return_partials=True)

      # Agrega el enunciado
        name = speaker_name + "_gen_%05d" % np.random.randint(100000)
        utterance = Utterance(name, speaker_name, wav, spec, embed, partial_embeds, True)
        self.utterances.add(utterance)

        # Plot it
        self.ui.draw_embed(embed, name, "generated")
        self.ui.draw_umap_projections(self.utterances)

    def init_encoder(self):
        model_fpath = self.ui.current_encoder_fpath

        self.ui.log("Loading the encoder %s... " % model_fpath)
        self.ui.set_loading(1)
        start = timer()
        encoder.load_model(model_fpath)
        self.ui.log("Done (%dms)." % int(1000 * (timer() - start)), "append")
        self.ui.set_loading(0)

    def init_synthesizer(self):
        model_fpath = self.ui.current_synthesizer_fpath

        self.ui.log("Loading the synthesizer %s... " % model_fpath)
        self.ui.set_loading(1)
        start = timer()
        self.synthesizer = Synthesizer(model_fpath)
        self.ui.log("Done (%dms)." % int(1000 * (timer() - start)), "append")
        self.ui.set_loading(0)

    def init_vocoder(self):
        model_fpath = self.ui.current_vocoder_fpath
        # Case  de  Griffin-lim
        if model_fpath is None:
            return

        self.ui.log("Loading the vocoder %s... " % model_fpath)
        self.ui.set_loading(1)
        start = timer()
        vocoder.load_model(model_fpath)
        self.ui.log("Done (%dms)." % int(1000 * (timer() - start)), "append")
        self.ui.set_loading(0)

    def update_seed_textbox(self):
       self.ui.update_seed_textbox()
