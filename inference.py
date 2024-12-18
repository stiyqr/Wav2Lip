import argparse
import math
import os
import platform
import subprocess

import cv2
import face_recognition
import numpy as np
import torch
from tqdm import tqdm
import re

import audio
# from face_detect import face_rect
from models import Wav2Lip

from batch_face import RetinaFace
from time import time

parser = argparse.ArgumentParser(description='Inference code to lip-sync videos in the wild using Wav2Lip models')

parser.add_argument('--checkpoint_path', type=str, 
                    help='Name of saved checkpoint to load weights from', required=True)

parser.add_argument('--face', type=str, 
                    help='Filepath of video/image that contains faces to use', required=True)
parser.add_argument('--audio', type=str, 
                    help='Filepath of video/audio file to use as raw audio source', required=True)
parser.add_argument('--speaker', type=str, help='Name of the speaker for this clip.', required=True)
parser.add_argument('--outfile', type=str, help='Video path to save result. See default for an e.g.', 
                                default='results/result_voice.mp4')

parser.add_argument('--static', type=bool, 
                    help='If True, then use only first video frame for inference', default=False)
parser.add_argument('--fps', type=float, help='Can be specified only if input is a static image (default: 25)', 
                    default=25., required=False)

parser.add_argument('--pads', nargs='+', type=int, default=[0, 10, 0, 0], 
                    help='Padding (top, bottom, left, right). Please adjust to include chin at least')

parser.add_argument('--wav2lip_batch_size', type=int, help='Batch size for Wav2Lip model(s)', default=128)

parser.add_argument('--resize_factor', default=1, type=int,
             help='Reduce the resolution by this factor. Sometimes, best results are obtained at 480p or 720p')

parser.add_argument('--out_height', default=480, type=int,
            help='Output video height. Best results are obtained at 480 or 720')

parser.add_argument('--crop', nargs='+', type=int, default=[0, -1, 0, -1],
                    help='Crop video to a smaller region (top, bottom, left, right). Applied after resize_factor and rotate arg. ' 
                    'Useful if multiple face present. -1 implies the value will be auto-inferred based on height, width')

parser.add_argument('--box', nargs='+', type=int, default=[-1, -1, -1, -1], 
                    help='Specify a constant bounding box for the face. Use only as a last resort if the face is not detected.'
                    'Also, might work only if the face is not moving around much. Syntax: (top, bottom, left, right).')

parser.add_argument('--rotate', default=False, action='store_true',
                    help='Sometimes videos taken from a phone can be flipped 90deg. If true, will flip video right by 90deg.'
                    'Use if you get a flipped result, despite feeding a normal looking video')

parser.add_argument('--nosmooth', default=False, action='store_true',
                    help='Prevent smoothing face detections over a short temporal window')

parser.add_argument('--image_paths', nargs='+', type=str, help='List of image file paths to process for face encodings', required=True)


def get_smoothened_boxes(boxes, T):
    for i in range(len(boxes)):
        if i + T > len(boxes):
            window = boxes[len(boxes) - T:]
        else:
            window = boxes[i : i + T]
        boxes[i] = np.mean(window, axis=0)
    return boxes


# img = cv2.imread("../man 1.png")
# rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
# img_encoding = face_recognition.face_encodings(rgb_img, num_jitters=2, model="large")[0]

# img2 = cv2.imread("../man 2.png")
# rgb_img2 = cv2.cvtColor(img2, cv2.COLOR_BGR2RGB)
# img_encoding2 = face_recognition.face_encodings(rgb_img2, num_jitters=2, model="large")[0]

# img3 = cv2.imread("../woman 1.png")
# rgb_img3 = cv2.cvtColor(img3, cv2.COLOR_BGR2RGB)
# img_encoding3 = face_recognition.face_encodings(rgb_img3, num_jitters=2, model="large")[0]

# img4 = cv2.imread("../woman 2.png")
# rgb_img4 = cv2.cvtColor(img4, cv2.COLOR_BGR2RGB)
# img_encoding4 = face_recognition.face_encodings(rgb_img4, num_jitters=2, model="large")[0]

# img5 = cv2.imread("../man 1-2.png")
# rgb_img5 = cv2.cvtColor(img5, cv2.COLOR_BGR2RGB)
# img_encoding5 = face_recognition.face_encodings(rgb_img5, num_jitters=2, model="large")[0]

# img6 = cv2.imread("../man 2-2.png")
# rgb_img6 = cv2.cvtColor(img6, cv2.COLOR_BGR2RGB)
# img_encoding6 = face_recognition.face_encodings(rgb_img6, num_jitters=2, model="large")[0]

# known_face_encodings = [
#     img_encoding,
#     img_encoding2,
#     img_encoding3,
#     img_encoding4,
#     img_encoding5,
#     img_encoding6
# ]
# known_face_names = [
#     "man 1",
#     "man 2",
#     "woman 1",
#     "woman 2",
#     "man 1",
#     "man 2"
# ]

known_face_encodings = []
known_face_names = []

def parse_face_name(file_path):
    """
    Extract the name from the file path based on the naming rule.
    File naming rule: "actor_name--(number to differentiate file)" NO SPACE!
    Example: "john--2"
    """
    file_name = os.path.basename(file_path)
    name = os.path.splitext(file_name)[0]  # Remove file extension
    
    # # Remove anything in parentheses, e.g., "man_1(2)" becomes "man_1"
    # name = re.sub(r"\s*\(.*\)$", "", name)
    # Remove anything after '--', e.g., "man_1--2" becomes "man_1"
    name = re.split(r"--", name)[0]
    return name

def append_face_data():
    """Append face encodings and names from the provided image paths to the known lists."""

    for img_path in args.image_paths:
        # Load the image
        img = cv2.imread(img_path)
        rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        # Get the face encoding
        try:
            img_encoding = face_recognition.face_encodings(rgb_img, num_jitters=2, model="large")[0]
        except IndexError:
            print(f"No face found in {img_path}, skipping...")
            continue

        # Parse the face name from the file name
        face_name = parse_face_name(img_path)

        # Append the face encoding and name to the known lists
        known_face_encodings.append(img_encoding)
        known_face_names.append(face_name)

def face_detect(images):
    results = []
    pady1, pady2, padx1, padx2 = args.pads

    s = time()

    # face_rect_multiple(images)

    for image, rect in zip(images, face_rect_multiple(images)):
        if rect is None:
            cv2.imwrite('temp/faulty_frame.jpg', image) # check this frame where the face was not detected.
            raise ValueError('Face not detected! Ensure the video contains a face in all the frames.')

        y1 = max(0, rect[1] - pady1)
        y2 = min(image.shape[0], rect[3] + pady2)
        x1 = max(0, rect[0] - padx1)
        x2 = min(image.shape[1], rect[2] + padx2)

        results.append([x1, y1, x2, y2])

        # ###########################################################
        # # cur_encoding = face_recognition.face_encodings(image)
        # # temp_res = face_recognition.compare_faces([img_encoding, img_encoding2], cur_encoding)

        # # if face_recognition.compare_faces([img_encoding], cur_encoding[0]):
        # #     cv2.putText(image, "GUY", (x1, y1 - 10), cv2.FONT_HERSHEY_DUPLEX, 1, (200, 0, 0), 2)
          
        # # if face_recognition.compare_faces([img_encoding2], cur_encoding[0]):
        # #     cv2.putText(image, "WOMAN", (x1, y1 - 10), cv2.FONT_HERSHEY_DUPLEX, 1, (200, 0, 0), 2)

        # face_locations = face_recognition.face_locations(image)
        # face_encodings = face_recognition.face_encodings(image, face_locations)

        # face_names = []
        # cur_name = ""
        # for face_encoding in face_encodings:
        #     # See if the face is a match for the known face(s)
        #     matches = face_recognition.compare_faces(known_face_encodings, face_encoding)
        #     name = "Unknown"

        #     # # If a match was found in known_face_encodings, just use the first one.
        #     if True in matches:
        #         first_match_index = matches.index(True)
        #         name = known_face_names[first_match_index]

        #     # Or instead, use the known face with the smallest distance to the new face
        #     # face_distances = face_recognition.face_distance(known_face_encodings, face_encoding)
        #     # best_match_index = np.argmin(face_distances)
        #     # if matches[best_match_index]:
        #     #     name = known_face_names[best_match_index]

        #     face_names.append(name)
        #     cur_name = name

        # cv2.putText(image, cur_name, (x1, y1 - 10), cv2.FONT_HERSHEY_DUPLEX, 1, (200, 0, 0), 2)
        # cv2.rectangle(image, (x1, y1), (x2, y2), (0, 0, 200), 2)
        # ###########################################################

    print('face detect time:', time() - s)

    boxes = np.array(results)
    if not args.nosmooth: boxes = get_smoothened_boxes(boxes, T=5)
    results = [[image[y1: y2, x1:x2], (y1, y2, x1, x2)] for image, (x1, y1, x2, y2) in zip(images, boxes)]

    return results


def datagen(frames, mels):
    img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

    if args.box[0] == -1:
        if not args.static:
            face_det_results = face_detect(frames) # BGR2RGB for CNN face detection
        else:
            face_det_results = face_detect([frames[0]])
    else:
        print('Using the specified bounding box instead of face detection...')
        y1, y2, x1, x2 = args.box
        face_det_results = [[f[y1: y2, x1:x2], (y1, y2, x1, x2)] for f in frames]

    for i, m in enumerate(mels):
        idx = 0 if args.static else i%len(frames)
        frame_to_save = frames[idx].copy()
        face, coords = face_det_results[idx].copy()

        face = cv2.resize(face, (args.img_size, args.img_size))

        img_batch.append(face)
        mel_batch.append(m)
        frame_batch.append(frame_to_save)
        coords_batch.append(coords)

        if len(img_batch) >= args.wav2lip_batch_size:
            img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

            img_masked = img_batch.copy()
            img_masked[:, args.img_size//2:] = 0

            img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
            mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

            yield img_batch, mel_batch, frame_batch, coords_batch
            img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

    if len(img_batch) > 0:
        img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

        img_masked = img_batch.copy()
        img_masked[:, args.img_size//2:] = 0

        img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
        mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

        yield img_batch, mel_batch, frame_batch, coords_batch

mel_step_size = 16
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Using {} for inference.'.format(device))

def _load(checkpoint_path):
    if device == 'cuda':
        checkpoint = torch.load(checkpoint_path)
    else:
        checkpoint = torch.load(checkpoint_path,
                                map_location=lambda storage, loc: storage)
    return checkpoint

def load_model(path):
    model = Wav2Lip()
    print("Load checkpoint from: {}".format(path))
    checkpoint = _load(path)
    s = checkpoint["state_dict"]
    new_s = {}
    for k, v in s.items():
        new_s[k.replace('module.', '')] = v
    model.load_state_dict(new_s)

    model = model.to(device)
    return model.eval()

def main():
    args.img_size = 96

    if os.path.isfile(args.face) and args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
        args.static = True

    if not os.path.isfile(args.face):
        raise ValueError('--face argument must be a valid path to video/image file')

    elif args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
        full_frames = [cv2.imread(args.face)]
        fps = args.fps

    else:
        video_stream = cv2.VideoCapture(args.face)
        fps = video_stream.get(cv2.CAP_PROP_FPS)

        print('Reading video frames...')

        full_frames = []
        while 1:
            still_reading, frame = video_stream.read()
            if not still_reading:
                video_stream.release()
                break

            aspect_ratio = frame.shape[1] / frame.shape[0]
            frame = cv2.resize(frame, (int(args.out_height * aspect_ratio), args.out_height))
            # if args.resize_factor > 1:
            #     frame = cv2.resize(frame, (frame.shape[1]//args.resize_factor, frame.shape[0]//args.resize_factor))

            if args.rotate:
                frame = cv2.rotate(frame, cv2.cv2.ROTATE_90_CLOCKWISE)

            y1, y2, x1, x2 = args.crop
            if x2 == -1: x2 = frame.shape[1]
            if y2 == -1: y2 = frame.shape[0]

            frame = frame[y1:y2, x1:x2]

            full_frames.append(frame)

    print ("Number of frames available for inference: "+str(len(full_frames)))

    if not args.audio.endswith('.wav'):
        print('Extracting raw audio...')
        # command = 'ffmpeg -y -i {} -strict -2 {}'.format(args.audio, 'temp/temp.wav')
        # subprocess.call(command, shell=True)
        subprocess.check_call([
            "ffmpeg", "-y",
            "-i", args.audio,
            "temp/temp.wav",
        ])
        args.audio = 'temp/temp.wav'

    wav = audio.load_wav(args.audio, 16000)
    mel = audio.melspectrogram(wav)
    print(mel.shape)

    if np.isnan(mel.reshape(-1)).sum() > 0:
        raise ValueError('Mel contains nan! Using a TTS voice? Add a small epsilon noise to the wav file and try again')

    mel_chunks = []
    mel_idx_multiplier = 80./fps
    i = 0
    while 1:
        start_idx = int(i * mel_idx_multiplier)
        if start_idx + mel_step_size > len(mel[0]):
            mel_chunks.append(mel[:, len(mel[0]) - mel_step_size:])
            break
        mel_chunks.append(mel[:, start_idx : start_idx + mel_step_size])
        i += 1

    print("Length of mel chunks: {}".format(len(mel_chunks)))

    full_frames = full_frames[:len(mel_chunks)]

    batch_size = args.wav2lip_batch_size
    gen = datagen(full_frames.copy(), mel_chunks)

    s = time()

    for i, (img_batch, mel_batch, frames, coords) in enumerate(tqdm(gen,
                                            total=int(np.ceil(float(len(mel_chunks))/batch_size)))):
        if i == 0:
            frame_h, frame_w = full_frames[0].shape[:-1]
            out = cv2.VideoWriter('temp/result.avi',
                                    cv2.VideoWriter_fourcc(*'DIVX'), fps, (frame_w, frame_h))

        img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
        mel_batch = torch.FloatTensor(np.transpose(mel_batch, (0, 3, 1, 2))).to(device)

        with torch.no_grad():
            pred = model(mel_batch, img_batch)

        pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.

        for p, f, c in zip(pred, frames, coords):
            y1, y2, x1, x2 = c
            p = cv2.resize(p.astype(np.uint8), (x2 - x1, y2 - y1))

            f[y1:y2, x1:x2] = p
            out.write(f)

    out.release()

    print("wav2lip prediction time:", time() - s)

    subprocess.check_call([
        "ffmpeg", "-y",
        # "-vsync", "0", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
        "-i", "temp/result.avi",
        "-i", args.audio,
        # "-c:v", "h264_nvenc",
        args.outfile,
    ])

model = detector = detector_model = None

def do_load(checkpoint_path):
    global model, detector, detector_model

    model = load_model(checkpoint_path)

    # SFDDetector.load_model(device)
    detector = RetinaFace(gpu_id=0, model_path="checkpoints/mobilenet.pth", network="mobilenet")
    # detector = RetinaFace(gpu_id=0, model_path="checkpoints/resnet50.pth", network="resnet50")

    detector_model = detector.model

    print("Models loaded")

def face_rect_multiple(images):
    prev_ret = None

    for image in images:
        face_locations = face_recognition.face_locations(image)
        face_encodings = face_recognition.face_encodings(image, face_locations, num_jitters=2, model="large")

        face_names = []
        similarities = []
        for face_encoding in face_encodings:
            # See if the face is a match for the known face(s)
            matches = face_recognition.compare_faces(known_face_encodings, face_encoding, tolerance=0.4)
            name = "Unknown"
            similarity = 0

            # # If a match was found in known_face_encodings, just use the first one.
            # if True in matches:
            #     first_match_index = matches.index(True)
            #     name = known_face_names[first_match_index]

            # Or instead, use the known face with the smallest distance to the new face
            face_distances = face_recognition.face_distance(known_face_encodings, face_encoding)
            best_match_index = np.argmin(face_distances)
            if matches[best_match_index]:
                similarity = 1 / (1 + face_distances[best_match_index])
                name = known_face_names[best_match_index]

            face_names.append(name)
            similarities.append(similarity)

        has_ret = False
        # Display the results
        for (top, right, bottom, left), name, percentage in zip(face_locations, face_names, similarities):
            # # Scale back up face locations since the frame we detected in was scaled to 1/4 size
            # top *= 4
            # right *= 4
            # bottom *= 4
            # left *= 4

            # Draw a box around the face
            cv2.rectangle(image, (left, top), (right, bottom), (0, 0, 255), 2)

            # Draw a label with a name below the face
            font = cv2.FONT_HERSHEY_DUPLEX
            # cv2.rectangle(image, (left, bottom - 35), (right, bottom), (0, 0, 255), cv2.FILLED)
            # cv2.putText(image, name, (left + 6, bottom - 6), font, 1.0, (255, 255, 255), 1)
            cv2.rectangle(image, (left, top), (right, bottom), (0, 0, 200), 2)
            
            if name == args.speaker:
                text_color = (0, 200, 0)
            else:
                text_color = (200, 0, 0)
            cv2.putText(image, name, (left, top - 10), font, 1, text_color, 2) # actor name
            cv2.putText(image, f"{percentage * 100:.1f}%", (left, bottom + 40), font, 1, text_color, 2) # similarity percentage

            if name == args.speaker:
                box_list = [left, top, right, bottom]
                box = np.array(box_list)
                prev_ret = tuple(map(int, box))
                has_ret = True
            elif not has_ret:
                # box_list = [left, top, right, bottom]
                # box = np.array(box_list)
                # prev_ret = tuple(map(int, box))
                prev_ret = None
        yield prev_ret

face_batch_size = 64 * 8

def face_rect(images):
    num_batches = math.ceil(len(images) / face_batch_size)
    prev_ret = None
    for i in range(num_batches):
        batch = images[i * face_batch_size: (i + 1) * face_batch_size]
        all_faces = detector(batch)  # return faces list of all images
        for faces in all_faces:
            if faces:
                box, landmarks, score = faces[0]
                prev_ret = tuple(map(int, box))
            yield prev_ret
            


if __name__ == '__main__':
    args = parser.parse_args()
    do_load(args.checkpoint_path)
    append_face_data()
    main()
