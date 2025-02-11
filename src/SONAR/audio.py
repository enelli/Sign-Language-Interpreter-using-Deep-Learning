from numpy.testing._private.utils import IgnoreException
import pyaudio
import wave
import numpy as np
import struct
import matplotlib.pyplot as plt
import time
from math import log
from threading import Thread

SAMPLE_RATE = 44100  # default audio sample rate
# dimensions of the threshold array to feed into visual ML
WIDTH = 300
HEIGHT = 300
BUFFER_SIZE = 2048
SOUND_SPEED = 343
THRESH_PROP = 1/38
THRESH = 1  # FFT threshold to filter out noise, will be THRESH_PROP * base amplitude
STALL_WINDOW_THRESH = 1  # number of stalled windows allowed within a single movement
ENABLE_DRAW = False  # whether to plot data

CALIBRATION_WINDOWS = 50  # number of windows to use for calibration
MIN_ALLOWED_AMP = 10  # minimum volume threshold to account for noise floor
MOV_AVG_ALPH = 0.2  # weighting factor for calibration average

class SONAR:
    ''' detect hand positions through SONAR '''
    def __init__(self, samp = SAMPLE_RATE):
        # audio parameters setup
        self.fs = samp  # audio sample rate
        self.chunk = BUFFER_SIZE
        self.p = pyaudio.PyAudio()
        self.num_channels = 1  # use mono output for now
        self.format = pyaudio.paFloat32

        # stream for signal output
        # 'output = True' indicates that the sound will be played rather than recorded
        # I have absolutely no idea what device_index is for but it prevents segfaults
        self.output_stream = self.p.open(format = self.format,
                                frames_per_buffer = self.chunk,
                                channels = self.num_channels,
                                rate = self.fs,
                                output = True,
                                output_device_index = None)
        # stream for receiving signals
        self.input_stream = self.p.open(format = self.format,
                                channels = self.num_channels,
                                rate = self.fs,
                                frames_per_buffer = self.chunk,
                                input = True,
                                input_device_index = None)

        # allow other threads to abort this one
        self.terminate = False

        # fft frequency window, will be clipped to readable frequencies
        self.f_vec = self.fs * np.arange(self.chunk)/self.chunk 
        # set indices on frequency range
        self.low_ind = 0
        self.high_ind = 0

        self.amp = 0.8  # amplitude for signal sending

        self.movement_flag = False
        self.movement_detected = False  # whether there is current motion
        self.move_count = 0  # most recent count of consecutive movement windows

    # allow camera thread to terminate audio threads
    def abort(self):
        self.terminate = True
                                
    def set_freq_range(self, low_freq, high_freq):
        self.low_ind = int(low_freq * self.chunk / self.fs)
        self.high_ind = int(high_freq * self.chunk / self.fs)
        self.f_vec = self.f_vec[self.low_ind:self.high_ind]

    # continuously play a tone at frequency freq
    def play_freq(self, freq):
        cur_frame = 0
        # signal: sin (2 pi * freq * time)
        while not self.terminate:
            # number of frames to produce on this iteration
            num_frames = self.output_stream.get_write_available()
            times = np.arange(cur_frame, cur_frame + num_frames) / self.fs
            arg = times * 2 * np.pi * freq
            # account for amplitude adjustments
            signal = self.amp * np.sin(arg)
            signal = signal.astype(np.float32)
            # log start of signal transmit as accurately as possible
            self.output_stream.write(signal.tobytes())
            cur_frame += num_frames

    # periodically transmit a constant frequency signal every interval seconds
    def transmit(self, freq, interval):
        #signal_length = 0.01  # still detectable
        signal_length = 2
        while not self.terminate:
            self.play_freq(freq, signal_length)
            time.sleep(interval - signal_length)

    def play(self, filename):
        # Open the sound file 
        wf = wave.open(filename, 'rb')

        if wf.getnchannels() != self.num_channels:
            raise Exception("Unsupported number of audio channels")

        # Read data in chunks
        data = wf.readframes(self.chunk)

        # Play the sound by writing the audio data to the stream
        # check for abort condition
        while data != b'' and not self.terminate:
            self.output_stream.write(data)
            data = wf.readframes(self.chunk)

        wf.close()

    # calibrate thresholds based on audio volume
    # also detect if volume is too low/muted
    # return True if everything is successful
    def calibrate_thresholds(self, freq):
        global THRESH  # TODO: don't do this :(
        # first fork a thread to play the frequency
        t = Thread(target = lambda: self.play_freq(freq))
        t.start()
        # very similar audio reading code to receive_burst
        cur_win = 0
        frames = []
        max_amp = 0
        success = True
        while cur_win < CALIBRATION_WINDOWS:
            num_frames = self.input_stream.get_read_available()
            input_signal = np.frombuffer(self.input_stream.read(num_frames, exception_on_overflow=False), dtype=np.float32)
            if len(input_signal) > 0:
                frames = np.concatenate((frames, input_signal))
            if len(frames) >= self.chunk:
                fft_data = np.abs(np.fft.rfft(frames[:self.chunk]))[self.low_ind:self.high_ind]
                if cur_win == 0:
                    max_amp = np.max(fft_data)
                else:
                    max_amp = max_amp * (1 - MOV_AVG_ALPH) + MOV_AVG_ALPH * np.max(fft_data)
                if max_amp < MIN_ALLOWED_AMP and cur_win >= 20:
                    print("Please increase your output volume")
                    success = False
                    break
                if ENABLE_DRAW and len(frames) < 1.5 * self.chunk:
                    fft_data = np.where(fft_data > THRESH, fft_data, 0)
                    plt.plot(self.f_vec, fft_data)
                    plt.draw()
                    plt.pause(1e-6)
                    plt.clf()
                frames = frames[self.chunk:]
                cur_win += 1
        self.terminate = True  # abort thread t
        if success: print("Calibration complete")
        t.join()
        THRESH = THRESH_PROP * max_amp
        self.terminate = False
        return success

    # detect time it takes for short signal to reach mic
    def receive_burst(self):
        frames = []
        prev_window = np.zeros(self.high_ind - self.low_ind)

        num_moves = 0  # number of consecutive windows with movement
        num_stall = 0  # number of consecutive windows without movement

        while not self.terminate:
            num_frames = self.input_stream.get_read_available()
            input_signal = np.frombuffer(self.input_stream.read(num_frames, exception_on_overflow=False), dtype=np.float32)
            if len(input_signal) > 0:
                frames = np.concatenate((frames, input_signal))
            # wait until we have a full chunk before processing; is this a good idea?
            if len(frames) >= self.chunk:  # wait until we have a full chunk before processing
                # fft_data[f] is now the amplitude? of the fth frequency (first two values are garbage)
                fft_data = np.abs(np.fft.rfft(frames[:self.chunk]))[self.low_ind:self.high_ind]
                # filter out low amplitudes
                fft_data = np.where(fft_data < THRESH, 0, fft_data)
                diff = np.abs(fft_data - prev_window)
                diff = np.where(diff < 2 * THRESH, 0, diff)

                # filter out single frequency peaks (these tend to be noise)
                if np.count_nonzero(diff) > 1:  # movement detected
                    num_moves += 1
                    self.movement_detected = True
                    num_stall = 0
                elif num_moves > 0:  # movement may have stopped
                    if num_stall < STALL_WINDOW_THRESH:
                        # allow one window of stopped movement
                        num_moves += 1
                    else:  # movement has stopped
                        self.movement_detected = False
                        self.movement_flag = True
                        print("Movement ended", num_moves)
                        # avoid trailing movement overwriting unread existing count
                        if num_moves > self.move_count:
                            self.move_count = num_moves
                        num_moves = 0
                    num_stall += 1

                # assuming near-ultrasound, the extracted frequency should be approximately the transmitted one
                #amp = max(fft_data)
                if ENABLE_DRAW and (len(frames) < 1.5 * self.chunk):  # do not draw every time
                    plt.plot(self.f_vec, diff)
                    plt.draw()
                    plt.pause(1e-6)
                    plt.clf()

                frames = frames[self.chunk:]  # clear frames already read
                prev_window = fft_data

    def is_moving(self):
        return self.movement_detected

    # read the most recent movement window count (0 if not moving)
    def read_move_count(self):
        count = self.move_count
        self.move_count = 0
        return count
            
    # record audio input and write to filename
    def record(self, filename):
        seconds = 10
        print('Recording')

        frames = []  # Initialize array to store frames

        # Store data in chunks for 3 seconds
        for i in range(0, int(self.fs / self.chunk * seconds)):
            if self.terminate: break
            data = self.input_stream.read(self.chunk)

            # Visualize: 
            # https://makersportal.com/blog/2018/9/17/audio-processing-in-python-part-ii-exploring-windowing-sound-pressure-levels-and-a-weighting-using-an-iphone-x
            data_int = np.array(struct.unpack(str(self.chunk*2) + 'B', data), dtype='b')[::2]
            fft_data = (np.abs(np.fft.fft(data_int))[0:int(np.floor(self.chunk/2))])/self.chunk
            fft_data[1:] = 2*fft_data[1:]
            plt.plot(self.f_vec,fft_data)
            
            frames.append(data)

        # Stop the stream
        self.input_stream.stop_stream()

        print('Finished recording')
        plt.show()

        # Save the recorded data as a WAV file
        wf = wave.open(filename, 'wb')
        wf.setnchannels(self.num_channels)
        wf.setsampwidth(self.p.get_sample_size(self.format))
        wf.setframerate(self.fs)
        wf.writeframes(b''.join(frames))
        wf.close()


    # Records two windows and subtracts them from each other
    def subtract_window(self):
        data = [self.input_stream.read(self.chunk) for _ in range(2)]
        data_int = [np.array(struct.unpack(str(self.chunk*2) + 'B', data[i]), dtype='b')[::2] for i in range(2)]
        fft_data = [(np.abs(np.fft.fft(data))[0:int(np.floor(self.chunk/2))])/self.chunk for data in data_int]
        plt.plot(self.f_vec, fft_data[0])
        plt.plot(self.f_vec, fft_data[1])
        fft_subtract = np.subtract(fft_data[1], fft_data[0])
        fft_subtract[1:] = 2*fft_subtract[1:]
        plt.plot(self.f_vec, fft_subtract)
        plt.show()

    # close all streams and terminate PortAudio interface
    def destruct(self):
        self.output_stream.close()
        self.input_stream.close()
        self.p.terminate()

    def detect_movement(self):
        self.set_freq_range(220, 1760)
        thread1 = Thread(target = lambda: self.play_freq(440))
        thread2 = Thread(target = self.receive_burst)

        thread1.start()
        thread2.start()
        thread1.join()
        thread2.join()

        self.destruct()
if __name__ == "__main__":
    s = SONAR()
    Thread(target=lambda:s.play_freq(19000)).start()
    s.set_freq_range(18000, 20000)
    s.receive_burst()