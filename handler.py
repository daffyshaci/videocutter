import subprocess
import runpod
import os
import tempfile
import shutil
import yt_dlp
import boto3
from botocore.exceptions import ClientError
from urllib.parse import urlparse

# --- Konfigurasi ---
# Preset NVENC: p1 (tercepat, kualitas terendah) -> p7 (terlambat, kualitas terbaik)
NVENC_PRESET = os.environ.get("NVENC_PRESET", "p3")

# Opsi yt-dlp
YTDL_OPTS = {
    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    'noplaylist': True,
    'quiet': True,
    'merge_output_format': 'mp4',
}

# --- Konfigurasi DigitalOcean Spaces (Baca dari Environment Variables) ---
DO_SPACES_ENDPOINT_URL = os.environ.get("DO_SPACES_ENDPOINT_URL") # Contoh: https://sgp1.digitaloceanspaces.com
DO_SPACES_REGION = os.environ.get("DO_SPACES_REGION") # Contoh: sgp1
DO_SPACES_ACCESS_KEY_ID = os.environ.get("DO_SPACES_ACCESS_KEY_ID")
DO_SPACES_SECRET_ACCESS_KEY = os.environ.get("DO_SPACES_SECRET_ACCESS_KEY")
DO_SPACES_BUCKET_NAME = os.environ.get("DO_SPACES_BUCKET_NAME")
# Opsional: Tentukan prefix folder di dalam bucket
DO_SPACES_UPLOAD_PREFIX = os.environ.get("DO_SPACES_UPLOAD_PREFIX", "output/")
# Opsional: Set ACL, 'public-read' membuat file bisa diakses via URL publik
# Pilihan lain: 'private' (default)
DO_SPACES_ACL = os.environ.get("DO_SPACES_ACL", "public-read")

# --- Validasi Konfigurasi DO Spaces ---
if not all([DO_SPACES_ENDPOINT_URL, DO_SPACES_REGION, DO_SPACES_ACCESS_KEY_ID, DO_SPACES_SECRET_ACCESS_KEY, DO_SPACES_BUCKET_NAME]):
    print("WARNING: Konfigurasi DigitalOcean Spaces tidak lengkap. Upload akan dilewati.")
    DO_SPACES_ENABLED = False
else:
    DO_SPACES_ENABLED = True
    print(f"DigitalOcean Spaces diaktifkan. Upload ke bucket: {DO_SPACES_BUCKET_NAME}, Region: {DO_SPACES_REGION}, Prefix: {DO_SPACES_UPLOAD_PREFIX}, ACL: {DO_SPACES_ACL}")
    # Inisialisasi client S3 untuk DO Spaces
    session = boto3.session.Session()
    s3_client = session.client('s3',
                               region_name=DO_SPACES_REGION,
                               endpoint_url=DO_SPACES_ENDPOINT_URL,
                               aws_access_key_id=DO_SPACES_ACCESS_KEY_ID,
                               aws_secret_access_key=DO_SPACES_SECRET_ACCESS_KEY)


# --- Fungsi Helper ---

def download_video(url, download_path):
    """Mengunduh video dari URL menggunakan yt-dlp."""
    print(f"Mulai mengunduh video dari: {url}")
    local_filename = os.path.join(download_path, 'input_video.mp4')
    opts = YTDL_OPTS.copy()
    opts['outtmpl'] = local_filename

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        downloaded_files = [f for f in os.listdir(download_path) if f.startswith('input_video') and f.endswith('.mp4')]
        if not downloaded_files:
             raise FileNotFoundError("File video yang diunduh tidak ditemukan.")
        actual_filename = os.path.join(download_path, downloaded_files[0])
        print(f"Video berhasil diunduh ke: {actual_filename}")
        return actual_filename
    except Exception as e:
        print(f"Error saat mengunduh video: {e}")
        raise

def cut_video_gpu(input_file, output_file, start_time, end_time):
    """Memotong video menggunakan ffmpeg dengan akselerasi GPU (NVENC)."""
    print(f"Memulai pemotongan video: {input_file} -> {output_file}")
    print(f"Start: {start_time}, End: {end_time}, Preset: {NVENC_PRESET}")

    command = [
        'ffmpeg',
        '-ss', str(start_time),
        '-to', str(end_time),
        '-i', input_file,
        '-map', '0:v:0?',
        '-map', '0:a:0?',
        '-c:v', 'h264_nvenc',
        '-preset', NVENC_PRESET,
        '-c:a', 'copy',
        '-y',
        output_file
    ]
    try:
        print(f"Menjalankan perintah FFmpeg: {' '.join(command)}")
        process = subprocess.run(command, check=True, capture_output=True, text=True)
        # Print stderr karena ffmpeg sering output info progres ke stderr
        print("FFmpeg stderr:")
        print(process.stderr)
        print(f"Video berhasil dipotong ke: {output_file}")
        return output_file
    except subprocess.CalledProcessError as e:
        print(f"Error saat menjalankan FFmpeg (return code: {e.returncode})")
        print("FFmpeg stdout:")
        print(e.stdout)
        print("FFmpeg stderr:")
        print(e.stderr)
        raise
    except Exception as e:
        print(f"Error tidak terduga saat pemotongan video: {e}")
        raise

def upload_to_spaces(local_file_path, bucket_name, object_name, acl=DO_SPACES_ACL):
    """Upload file ke DigitalOcean Spaces."""
    if not DO_SPACES_ENABLED:
        print("Upload ke DO Spaces dilewati karena konfigurasi tidak lengkap.")
        return None

    print(f"Mengupload {local_file_path} ke s3://{bucket_name}/{object_name} dengan ACL: {acl}")
    try:
        # Menentukan ContentType agar bisa diputar langsung di browser
        content_type = 'video/mp4'
        extra_args = {'ACL': acl, 'ContentType': content_type}

        s3_client.upload_file(local_file_path, bucket_name, object_name, ExtraArgs=extra_args)
        print(f"Upload berhasil: {object_name}")

        # Konstruksi URL Publik
        # Format URL: https://{bucket_name}.{region}.digitaloceanspaces.com/{object_name}
        public_url = f"{DO_SPACES_ENDPOINT_URL}/{bucket_name}/{object_name}"
        print(f"URL Publik: {public_url}")
        return public_url

    except ClientError as e:
        print(f"Error saat upload ke DO Spaces: {e}")
        # Log error lebih detail jika perlu
        # import logging
        # logging.error(e)
        return None
    except Exception as e:
        print(f"Error tidak terduga saat upload: {e}")
        return None


# --- Handler RunPod ---

def handler(job):
    """
    Handler utama untuk job RunPod Serverless.
    """
    job_input = job.get('input', None)

    if not job_input:
        return {"error": "Input tidak ditemukan dalam job payload."}

    youtube_url = job_input.get('youtube_url')
    start_time = job_input.get('start_time')
    end_time = job_input.get('end_time')

    if not youtube_url or start_time is None or end_time is None:
        return {"error": "Parameter 'youtube_url', 'start_time', dan 'end_time' wajib ada."}

    temp_dir = tempfile.mkdtemp()
    print(f"Direktori kerja sementara dibuat: {temp_dir}")
    output_url = None # Inisialisasi

    try:
        # 1. Unduh video
        downloaded_video_path = download_video(youtube_url, temp_dir)

        # 2. Siapkan path output lokal
        base_filename = os.path.basename(downloaded_video_path).split('.')[0]
        # Buat nama file output yang lebih bersih
        safe_start_time = str(start_time).replace(":", "-").replace(".", "_")
        safe_end_time = str(end_time).replace(":", "-").replace(".", "_")
        output_filename_local = f"cut_{base_filename}_{safe_start_time}_{safe_end_time}.mp4"
        output_video_path_local = os.path.join(temp_dir, output_filename_local)

        # 3. Potong video menggunakan GPU
        cut_video_gpu(downloaded_video_path, output_video_path_local, start_time, end_time)

        # 4. Upload hasil ke DigitalOcean Spaces
        if DO_SPACES_ENABLED:
            # Tentukan nama objek (key) di bucket Spaces
            # Pastikan prefix diakhiri '/' jika ada
            object_key = DO_SPACES_UPLOAD_PREFIX
            if not object_key.endswith('/'):
                object_key += '/'
            object_key += output_filename_local # Gunakan nama file lokal sebagai basis

            output_url = upload_to_spaces(output_video_path_local, DO_SPACES_BUCKET_NAME, object_key)

            if not output_url:
                 # Jika upload gagal, kembalikan error spesifik upload
                 return {"error": f"Video berhasil dipotong tetapi gagal diupload ke DigitalOcean Spaces."}
        else:
            print("Upload ke DO Spaces tidak dilakukan karena tidak dikonfigurasi.")
            # Jika DO Spaces tidak aktif, kita tidak bisa mengembalikan URL
            return {
                 "message": "Video berhasil dipotong, tetapi tidak diupload (DO Spaces tidak dikonfigurasi).",
                 "output_filename_temporary": output_filename_local
            }


        # 5. Kembalikan hasil (URL jika upload berhasil)
        print(f"Proses selesai. Output URL: {output_url}")
        return {
            "message": "Video berhasil dipotong dan diupload.",
            "output_url": output_url,
        }

    except FileNotFoundError as e:
         print(f"Error: {e}")
         return {"error": f"Gagal menemukan file: {e}"}
    except yt_dlp.utils.DownloadError as e:
        print(f"Error Download: {e}")
        # Mencoba mengambil pesan error yang lebih spesifik jika ada
        err_msg = str(e)
        if hasattr(e, 'msg'): err_msg = e.msg
        return {"error": f"Gagal mengunduh video: {err_msg}"}
    except subprocess.CalledProcessError as e:
        print(f"Error FFmpeg: {e}")
        # Mengembalikan stderr bisa membantu debugging
        return {"error": f"Gagal memproses video dengan FFmpeg. Stderr: {e.stderr}"}
    except ClientError as e:
         print(f"Error Boto3/Spaces: {e}")
         # Jangan bocorkan detail error spesifik ke klien
         return {"error": "Terjadi kesalahan saat berinteraksi dengan penyimpanan objek."}
    except Exception as e:
        print(f"Error tak terduga: {e}")
        import traceback
        traceback.print_exc() # Print traceback ke log server untuk debugging
        return {"error": f"Terjadi kesalahan internal: {type(e).__name__}"}
    finally:
        # Bersihkan direktori sementara
        if os.path.exists(temp_dir):
            print(f"Membersihkan direktori sementara: {temp_dir}")
            shutil.rmtree(temp_dir)

# Mulai server RunPod jika script dijalankan langsung
if __name__ == "__main__":
    print("Memulai worker RunPod...")
    runpod.serverless.start({"handler": handler})