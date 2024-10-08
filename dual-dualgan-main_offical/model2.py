from __future__ import division
import os
import time
from glob import glob
import tensorflow._api.v2.compat.v1 as tf
import numpy as np
#from six.moves import xrange

from ops import *
from utils import *

class DualNet(object):
    def __init__(self, sess, image_size=256, batch_size=1,gcn = 64, dcn=64, \
                 B_channels = 3, C_channels = 3, dataset_name='facades', \
                 checkpoint_dir=None, lambda_B = 500., lambda_C = 500., \
                 sample_dir=None, dropout_rate=0.0, loss_metric = 'L1', flip = False,\
                 n_critic = 5, GAN_type = 'wgan-gp', clip_value = 0.1, log_freq=50, disc_type = 'globalgan'):
        self.dcn = dcn
        self.flip = flip
        self.lambda_B = lambda_B
        self.lambda_C = lambda_C
        
        self.sess = sess
        self.is_grayscale_B = (B_channels == 1)
        self.is_grayscale_C = (C_channels == 1)
        self.batch_size = batch_size
        self.image_size = image_size
        self.gcn = gcn
        self.B_channels = B_channels
        self.C_channels = C_channels
        self.loss_metric = loss_metric

        self.dataset_name = dataset_name
        self.checkpoint_dir = checkpoint_dir
        
        #directory name for output and logs saving
        self.dir_name = "%s-img_sz_%s-fltr_dim_%d-%s-lambda_BC_%s_%s" % (
                    self.dataset_name, 
                    self.image_size,
                    self.gcn,
                    self.loss_metric, 
                    self.lambda_B, 
                    self.lambda_C
                ) 
        self.dropout_rate = dropout_rate
        self.clip_value = clip_value
        self.GAN_type = GAN_type
        self.n_critic = n_critic
        self.log_freq = log_freq
        self.gamma = 10.
        self.disc_type = disc_type
        self.build_model()

    def build_model(self):
    ###    define place holders
        self.real_B = tf.placeholder(tf.float32,[self.batch_size, self.image_size, self.image_size,
                                         self.B_channels ],name='real_B')
        self.real_C = tf.placeholder(tf.float32, [self.batch_size, self.image_size, self.image_size,
                                         self.C_channels ], name='real_C')
        
    ###  define graphs
        self.B2C = self.B_g_net(self.real_B, reuse = False)
        self.C2B = self.C_g_net(self.real_C, reuse = False)
        self.B2C2B = self.C_g_net(self.B2C, reuse = True)
        self.C2B2C = self.B_g_net(self.C2B, reuse = True)
        
        if self.loss_metric == 'L1':
            self.B_loss = tf.reduce_mean(tf.abs(self.B2C2B - self.real_B))
            self.C_loss = tf.reduce_mean(tf.abs(self.C2B2C - self.real_C))
        elif self.loss_metric == 'L2':
            self.B_loss = tf.reduce_mean(tf.square(self.B2C2B - self.real_B))
            self.C_loss = tf.reduce_mean(tf.square(self.C2B2C - self.real_C))
        
        self.Bd_logits_fake = self.B_d_net(self.B2C, reuse = False)
        self.Bd_logits_real = self.B_d_net(self.real_C, reuse = True)

        if self.GAN_type == 'wgan-gp':
            epsilon_C = tf.random_uniform(
                shape=[self.batch_size, 1, 1, 1], minval=0., maxval=1.)
            interpolated_image_C = self.real_C + epsilon_C * (self.B2C - self.real_C)
            d_interpolated_C = self.B_d_net(interpolated_image_C, reuse = True)

        if self.GAN_type == 'classic':
            self.Bd_loss_real = celoss(self.Bd_logits_real, tf.ones_like(self.Bd_logits_real))
            self.Bd_loss_fake = celoss(self.Bd_logits_fake, tf.zeros_like(self.Bd_logits_fake))
        else:
            self.Bd_loss_real = -tf.reduce_mean(self.Bd_logits_real)
            self.Bd_loss_fake = tf.reduce_mean(self.Bd_logits_fake)
        self.Bd_loss = self.Bd_loss_fake + self.Bd_loss_real
        if self.GAN_type == 'classic':
            self.Bg_loss = celoss(self.Bd_logits_fake, labels=tf.ones_like(self.Bd_logits_fake))+self.lambda_C * self.C_loss
        else:
            self.Bg_loss = - tf.reduce_mean(self.Bd_logits_fake)+self.lambda_C * self.C_loss

        self.Cd_logits_fake = self.C_d_net(self.C2B, reuse = False)
        self.Cd_logits_real = self.C_d_net(self.real_B, reuse = True)
        if self.GAN_type == 'wgan-gp':
            epsilon_B = tf.random_uniform(
                shape=[self.batch_size, 1, 1, 1], minval=0., maxval=1.)
            interpolated_image_B = self.real_B + epsilon_B * (self.C2B - self.real_B)
            d_interpolated_B = self.C_d_net(interpolated_image_B, reuse = True)

        if self.GAN_type == 'classic':
            self.Cd_loss_real = celoss(self.Cd_logits_real, tf.ones_like(self.Cd_logits_real))
            self.Cd_loss_fake = celoss(self.Cd_logits_fake, tf.zeros_like(self.Cd_logits_fake))
        else:
            self.Cd_loss_real = -tf.reduce_mean(self.Cd_logits_real)
            self.Cd_loss_fake = tf.reduce_mean(self.Cd_logits_fake)
        self.Cd_loss = self.Cd_loss_fake + self.Cd_loss_real
        if self.GAN_type == 'classic':
            self.Cg_loss = celoss(self.Cd_logits_fake, tf.ones_like(self.Cd_logits_fake))+self.lambda_B * self.B_loss
        else:
            self.Cg_loss = -tf.reduce_mean(self.Cd_logits_fake)+self.lambda_B * self.B_loss


        self.d_loss = self.Bd_loss + self.Cd_loss
        self.g_loss = self.Bg_loss + self.Cg_loss

        if self.GAN_type == 'wgan-gp':
            grad_d_interp = tf.gradients(
                d_interpolated_B, [interpolated_image_B])[0]
            slopes = tf.sqrt(1e-8 + tf.reduce_sum(
                tf.square(grad_d_interp), axis=[1, 2, 3]))
            gradient_penalty = tf.reduce_mean((slopes - 1.) ** 2)
            self.d_loss += self.gamma * gradient_penalty

            grad_d_interp = tf.gradients(
                d_interpolated_C, [interpolated_image_C])[0]
            slopes = tf.sqrt(1e-8 + tf.reduce_sum(
                tf.square(grad_d_interp), axis=[1, 2, 3]))
            gradient_penalty = tf.reduce_mean((slopes - 1.) ** 2)
            self.d_loss += self.gamma * gradient_penalty


        ## define trainable variables
        t_vars = tf.trainable_variables()
        self.B_d_vars = [var for var in t_vars if 'B_d_' in var.name]
        self.C_d_vars = [var for var in t_vars if 'C_d_' in var.name]
        self.B_g_vars = [var for var in t_vars if 'B_g_' in var.name]
        self.C_g_vars = [var for var in t_vars if 'C_g_' in var.name]
        self.d_vars = self.B_d_vars + self.C_d_vars 
        self.g_vars = self.B_g_vars + self.C_g_vars
        self.saver = tf.train.Saver()

    def load_random_samples(self):
        #np.random.choice(
        sample_files =np.random.choice(glob('./datasets/{}/val/B/*.*[g|G]'.format(self.dataset_name)),self.batch_size)
        sample_B_imgs = [load_data(f, image_size =self.image_size, flip = False) for f in sample_files]
        
        sample_files = np.random.choice(glob('./datasets/{}/val/C/*.*[g|G]'.format(self.dataset_name)),self.batch_size)
        sample_C_imgs = [load_data(f, image_size =self.image_size, flip = False) for f in sample_files]

        sample_B_imgs = np.reshape(np.array(sample_B_imgs).astype(np.float32),(self.batch_size,self.image_size, self.image_size,-1))
        sample_C_imgs = np.reshape(np.array(sample_C_imgs).astype(np.float32),(self.batch_size,self.image_size, self.image_size,-1))
        return sample_B_imgs, sample_C_imgs

    def sample_shotcut(self, sample_dir, epoch_idx, batch_idx):
        sample_B_imgs,sample_C_imgs = self.load_random_samples()
        
        Bg, B2C2B_imgs, B2C_imgs = self.sess.run([self.B_loss, self.B2C2B, self.B2C], feed_dict={self.real_B: sample_B_imgs, self.real_C: sample_C_imgs})
        Cg, C2B2C_imgs, C2B_imgs = self.sess.run([self.C_loss, self.C2B2C, self.C2B], feed_dict={self.real_B: sample_B_imgs, self.real_C: sample_C_imgs})

        save_images(B2C_imgs, [self.batch_size,1], './{}/{}/{:06d}_{:04d}_B2C.jpg'.format(sample_dir,self.dir_name , epoch_idx, batch_idx))
        save_images(B2C2B_imgs, [self.batch_size,1],    './{}/{}/{:06d}_{:04d}_B2C2B.jpg'.format(sample_dir,self.dir_name, epoch_idx,  batch_idx))
        
        save_images(C2B_imgs, [self.batch_size,1], './{}/{}/{:06d}_{:04d}_C2B.jpg'.format(sample_dir,self.dir_name, epoch_idx, batch_idx))
        save_images(C2B2C_imgs, [self.batch_size,1], './{}/{}/{:06d}_{:04d}_C2B2C.jpg'.format(sample_dir,self.dir_name, epoch_idx, batch_idx))
        
        print("[Sample] B_loss: {:.8f}, C_loss: {:.8f}".format(Bg, Cg))

    def train(self, args):
        """Train Dual GAN"""
        decay = 0.9
        self.d_optim = tf.train.RMSPropOptimizer(args.lr, decay=decay) \
                          .minimize(self.d_loss, var_list=self.d_vars)
                          
        self.g_optim = tf.train.RMSPropOptimizer(args.lr, decay=decay) \
                          .minimize(self.g_loss, var_list=self.g_vars)          
        tf.global_variables_initializer().run()
        if self.GAN_type == 'wgan':
            self.clip_ops =  [var.assign(tf.clip_by_value(var, -self.clip_value, self.clip_value)) for var in self.d_vars]

        self.writer = tf.summary.FileWriter("./logs/"+self.dir_name, self.sess.graph)

        step = 1
        start_time = time.time()

        if self.load(self.checkpoint_dir):
            print(" [*] Load SUCCESS")
        else:
            print(" Load failed...ignored...")
            print(" start training...")

        for epoch_idx in range(args.epoch):
            data_B = glob('./datasets/{}/train/B/*.*[g|G]'.format(self.dataset_name))
            data_C = glob('./datasets/{}/train/C/*.*[g|G]'.format(self.dataset_name))
            np.random.shuffle(data_B)
            np.random.shuffle(data_C)
            epoch_size = min(len(data_B), len(data_C)) // (self.batch_size)
            print('[*] training data loaded successfully')
            print("#data_B: %d  #data_C:%d" %(len(data_B),len(data_C)))
            print('[*] run optimizor...')

            for batch_idx in range(0, epoch_size):
                imgB_batch = self.load_training_imgs(data_B, batch_idx)
                imgC_batch = self.load_training_imgs(data_C, batch_idx)
                if step % self.log_freq == 0:
                    print("Epoch: [%2d] [%4d/%4d]"%(epoch_idx, batch_idx, epoch_size))
                step = step + 1
                self.run_optim(imgB_batch, imgC_batch, step, start_time, step)

                if np.mod(step, 100) == 1:
                    self.sample_shotcut(args.sample_dir, epoch_idx, batch_idx)

                if np.mod(step, args.save_freq) == 2:
                    self.save(args.checkpoint_dir, step)

    def load_training_imgs(self, files, idx):
        batch_files = files[idx*self.batch_size:(idx+1)*self.batch_size]
        batch_imgs = [load_data(f, image_size =self.image_size, flip = self.flip) for f in batch_files]
                
        batch_imgs = np.reshape(np.array(batch_imgs).astype(np.float32),(self.batch_size,self.image_size, self.image_size,-1))
        
        return batch_imgs
        
    def run_optim(self,batch_B_imgs, batch_C_imgs,  counter, start_time, batch_idx):
        

        _, Bdfake,Bdreal,Cdfake,Cdreal, Bd, Cd = self.sess.run(
            [self.d_optim, self.Bd_loss_fake, self.Bd_loss_real, self.Cd_loss_fake, self.Cd_loss_real, self.Bd_loss, self.Cd_loss], 
            feed_dict = {self.real_B: batch_B_imgs, self.real_C: batch_C_imgs})
        
        if 'wgan' == self.GAN_type:
        	self.sess.run(self.clip_ops)

        if 'wgan' in self.GAN_type:
            if batch_idx % self.n_critic == 0:
                _, Bg, Cg, Bloss, Closs = self.sess.run(
                [self.g_optim, self.Bg_loss, self.Cg_loss, self.B_loss, self.C_loss], 
                feed_dict={ self.real_B: batch_B_imgs, self.real_C: batch_C_imgs})
            else:
                Bg, Cg, Bloss, Closs = self.sess.run(
                [self.Bg_loss, self.Cg_loss, self.B_loss, self.C_loss], 
                feed_dict={ self.real_B: batch_B_imgs, self.real_C: batch_C_imgs})
        else:
            _, Bg, Cg, Bloss, Closs = self.sess.run(
                [self.g_optim, self.Bg_loss, self.Cg_loss, self.B_loss, self.C_loss], 
                feed_dict={ self.real_B: batch_B_imgs, self.real_C: batch_C_imgs})
            _, Bg, Cg, Bloss, Closs = self.sess.run(
                [self.g_optim, self.Bg_loss, self.Cg_loss, self.B_loss, self.C_loss], 
                feed_dict={ self.real_B: batch_B_imgs, self.real_C: batch_C_imgs})
        if batch_idx % self.log_freq == 0:
            print("time: %4.4f, Bd: %.2f, Bg: %.2f, Cd: %.2f, Cg: %.2f,  U_diff: %.5f, V_diff: %.5f" \
                    % (time.time() - start_time, Bd,Bg,Cd,Cg, Bloss, Closs))
            print("Bd_fake: %.2f, Bd_real: %.2f, Cd_fake: %.2f, Cd_real: %.2f" % (Bdfake,Bdreal,Cdfake,Cdreal))

    def B_d_net(self, imgs, y = None, reuse = False):
        return self.discriminator(imgs, prefix = 'B_d_', reuse = reuse)
    
    def C_d_net(self, imgs, y = None, reuse = False):
        return self.discriminator(imgs, prefix = 'C_d_', reuse = reuse)
        
    def discriminator(self, image,  y=None, prefix='B_d_', reuse=False):
        # image is 256 x 256 x (input_c_dim + output_c_dim)
        with tf.variable_scope(tf.get_variable_scope()) as scope:
            if reuse:
                scope.reuse_variables()
            else:
                assert scope.reuse == False

            h0 = lrelu(conv2d(image, self.dcn, k_h=5, k_w=5, name=prefix+'h0_conv'))
            # h0 is (128 x 128 x self.dcn)
            h1 = lrelu(batch_norm(conv2d(h0, self.dcn*2, name=prefix+'h1_conv'), name = prefix+'bn1'))
            # h1 is (64 x 64 x self.dcn*2)
            h2 = lrelu(batch_norm(conv2d(h1, self.dcn*4, name=prefix+'h2_conv'), name = prefix+ 'bn2'))
            # h2 is (32x 32 x self.dcn*4)
            h3 = lrelu(batch_norm(conv2d(h2, self.dcn*8, name=prefix+'h3_conv'), name = prefix+ 'bn3'))
            # h3 is (16 x 16 x self.dcn*8)
            h3 = lrelu(batch_norm(conv2d(h3, self.dcn*8, name=prefix+'h3_1_conv'), name = prefix+ 'bn3_1'))
            # h3 is (8 x 8 x self.dcn*8)

            if self.disc_type == 'patchgan':
                h4 = conv2d(h3, 1, name =prefix+'h4')
            else:
                h4 = linear(tf.reshape(h3, [self.batch_size, -1]), 1, prefix+'d_h3_lin')

            return h4
        
    def B_g_net(self, imgs, reuse=False):
        return self.fcn(imgs, prefix='B_g_', reuse = reuse)
        

    def C_g_net(self, imgs, reuse=False):
        return self.fcn(imgs, prefix = 'C_g_', reuse = reuse)
        
    def fcn(self, imgs, prefix=None, reuse = False):
        with tf.variable_scope(tf.get_variable_scope()) as scope:
            if reuse:
                scope.reuse_variables()
            else:
                assert scope.reuse == False
            
            s = self.image_size
            s2, s4, s8, s16, s32, s64, s128 = int(s/2), int(s/4), int(s/8), int(s/16), int(s/32), int(s/64), int(s/128)

            # imgs is (256 x 256 x input_c_dim)
            e1 = conv2d(imgs, self.gcn, k_h=5, k_w=5, name=prefix+'e1_conv')
            # e1 is (128 x 128 x self.gcn)
            e2 = batch_norm(conv2d(lrelu(e1), self.gcn*2, name=prefix+'e2_conv'), name = prefix+'bn_e2')
            # e2 is (64 x 64 x self.gcn*2)
            e3 = batch_norm(conv2d(lrelu(e2), self.gcn*4, name=prefix+'e3_conv'), name = prefix+'bn_e3')
            # e3 is (32 x 32 x self.gcn*4)
            e4 = batch_norm(conv2d(lrelu(e3), self.gcn*8, name=prefix+'e4_conv'), name = prefix+'bn_e4')
            # e4 is (16 x 16 x self.gcn*8)
            e5 = batch_norm(conv2d(lrelu(e4), self.gcn*8, name=prefix+'e5_conv'), name = prefix+'bn_e5')
            # e5 is (8 x 8 x self.gcn*8)
            e6 = batch_norm(conv2d(lrelu(e5), self.gcn*8, name=prefix+'e6_conv'), name = prefix+'bn_e6')
            # e6 is (4 x 4 x self.gcn*8)
            e7 = batch_norm(conv2d(lrelu(e6), self.gcn*8, name=prefix+'e7_conv'), name = prefix+'bn_e7')
            # e7 is (2 x 2 x self.gcn*8)
            e8 = batch_norm(conv2d(lrelu(e7), self.gcn*8, name=prefix+'e8_conv'), name = prefix+'bn_e8')
            # e8 is (1 x 1 x self.gcn*8)

            self.d1, self.d1_w, self.d1_b = deconv2d(tf.nn.relu(e8),
                [self.batch_size, s128, s128, self.gcn*8], name=prefix+'d1', with_w=True)
            if self.dropout_rate <= 0.:
                d1 = batch_norm(self.d1, name = prefix+'bn_d1')
            else:
                d1 = tf.nn.dropout(batch_norm(self.d1, name = prefix+'bn_d1'), self.dropout_rate)
            d1 = tf.concat([d1, e7],3)
            # d1 is (2 x 2 x self.gcn*8*2)

            self.d2, self.d2_w, self.d2_b = deconv2d(tf.nn.relu(d1),
                [self.batch_size, s64, s64, self.gcn*8], name=prefix+'d2', with_w=True)
            if self.dropout_rate <= 0.:
                d2 = batch_norm(self.d2, name = prefix+'bn_d2')
            else:
                d2 = tf.nn.dropout(batch_norm(self.d2, name = prefix+'bn_d2'), self.dropout_rate)
            d2 = tf.concat([d2, e6],3)
            # d2 is (4 x 4 x self.gcn*8*2)

            self.d3, self.d3_w, self.d3_b = deconv2d(tf.nn.relu(d2),
                [self.batch_size, s32, s32, self.gcn*8], name=prefix+'d3', with_w=True)
            if self.dropout_rate <= 0.:
                d3 = batch_norm(self.d3, name = prefix+'bn_d3')
            else:
                d3 = tf.nn.dropout(batch_norm(self.d3, name = prefix+'bn_d3'), self.dropout_rate)
            d3 = tf.concat([d3, e5],3)
            # d3 is (8 x 8 x self.gcn*8*2)

            self.d4, self.d4_w, self.d4_b = deconv2d(tf.nn.relu(d3),
                [self.batch_size, s16, s16, self.gcn*8], name=prefix+'d4', with_w=True)
            d4 = batch_norm(self.d4, name = prefix+'bn_d4')

            d4 = tf.concat([d4, e4],3)
            # d4 is (16 x 16 x self.gcn*8*2)

            self.d5, self.d5_w, self.d5_b = deconv2d(tf.nn.relu(d4),
                [self.batch_size, s8, s8, self.gcn*4], name=prefix+'d5', with_w=True)
            d5 = batch_norm(self.d5, name = prefix+'bn_d5')
            d5 = tf.concat([d5, e3],3)
            # d5 is (32 x 32 x self.gcn*4*2)

            self.d6, self.d6_w, self.d6_b = deconv2d(tf.nn.relu(d5),
                [self.batch_size, s4, s4, self.gcn*2], name=prefix+'d6', with_w=True)
            d6 = batch_norm(self.d6, name = prefix+'bn_d6')
            d6 = tf.concat([d6, e2],3)
            # d6 is (64 x 64 x self.gcn*2*2)

            self.d7, self.d7_w, self.d7_b = deconv2d(tf.nn.relu(d6),
                [self.batch_size, s2, s2, self.gcn], name=prefix+'d7', with_w=True)
            d7 = batch_norm(self.d7, name = prefix+'bn_d7')
            d7 = tf.concat([d7, e1],3)
            # d7 is (128 x 128 x self.gcn*1*2)

            if prefix == 'C_g_':
                self.d8, self.d8_w, self.d8_b = deconv2d(tf.nn.relu(d7),[self.batch_size, s, s, self.B_channels], k_h=5, k_w=5, name=prefix+'d8', with_w=True)
            elif prefix == 'B_g_':
                self.d8, self.d8_w, self.d8_b = deconv2d(tf.nn.relu(d7),[self.batch_size, s, s, self.C_channels], k_h=5, k_w=5, name=prefix+'d8', with_w=True)
             # d8 is (256 x 256 x output_c_dim)
            return tf.nn.tanh(self.d8)
    
    def save(self, checkpoint_dir, step):
        model_name = "DualNet.model"
        model_dir = self.dir_name
        checkpoint_dir = os.path.join(checkpoint_dir, model_dir)

        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)

        self.saver.save(self.sess,
                        os.path.join(checkpoint_dir, model_name),
                        global_step=step)

    def load(self, checkpoint_dir):
        print(" [*] Reading checkpoint...")

        model_dir =  self.dir_name
        checkpoint_dir = os.path.join(checkpoint_dir, model_dir)

        ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            self.saver.restore(self.sess, os.path.join(checkpoint_dir, ckpt_name))
            return True
        else:
            return False

    def test(self, args):
        """Test DualNet"""
        start_time = time.time()
        tf.global_variables_initializer().run()
        if self.load(self.checkpoint_dir):
            print(" [*] Load SUCCESS")
            test_dir = './{}/{}'.format(args.test_dir, self.dir_name)
            if not os.path.exists(test_dir):
                os.makedirs(test_dir)
            test_log = open(test_dir+'evaluation.txt','a') 
            test_log.write(self.dir_name)
            self.test_domain(args, test_log, type = 'B')
            self.test_domain(args, test_log, type = 'C')
            test_log.close()
        
    def test_domain(self, args, test_log, type = 'B'):
        test_files = glob('./datasets/{}/val/{}/*.*[g|G]'.format(self.dataset_name,type))
        # load testing input
        print("Loading testing images ...")
        test_imgs = [load_data(f, is_test=True, image_size =self.image_size, flip = args.flip) for f in test_files]
        print("#images loaded: %d"%(len(test_imgs)))
        test_imgs = np.reshape(np.asarray(test_imgs).astype(np.float32),(len(test_files),self.image_size, self.image_size,-1))
        test_imgs = [test_imgs[i*self.batch_size:(i+1)*self.batch_size]
                         for i in range(0, len(test_imgs)//self.batch_size)]
        test_imgs = np.asarray(test_imgs)
        test_path = './{}/{}/'.format(args.test_dir, self.dir_name)
        # test input samples
        if type == 'B':
            for i in range(0, len(test_files)//self.batch_size):
                filename_o = test_files[i*self.batch_size].split('/')[-1].split('.')[0]
                print(filename_o)
                idx = i+1
                B_imgs = np.reshape(np.array(test_imgs[i]), (self.batch_size,self.image_size, self.image_size,-1))
                print("testing B image %d"%(idx))
                print(B_imgs.shape)
                B2C_imgs, B2C2B_imgs = self.sess.run(
                    [self.B2C, self.B2C2B],
                    feed_dict={self.real_B: B_imgs}
                    )
                save_images(B_imgs, [self.batch_size, 1], test_path+filename_o+'_realB.jpg')
                save_images(B2C_imgs, [self.batch_size, 1], test_path+filename_o+'_B2C.jpg')
                save_images(B2C2B_imgs, [self.batch_size, 1], test_path+filename_o+'_B2C2B.jpg')
        elif type=='C':
            for i in range(0, len(test_files)//self.batch_size):
                filename_o = test_files[i*self.batch_size].split('/')[-1].split('.')[0]
                idx = i+1
                C_imgs = np.reshape(np.array(test_imgs[i]), (self.batch_size,self.image_size, self.image_size,-1))
                print("testing C image %d"%(idx))
                C2B_imgs, C2B2C_imgs = self.sess.run(
                    [self.C2B, self.C2B2C],
                    feed_dict={self.real_C:C_imgs}
                    )
                save_images(C_imgs, [self.batch_size, 1],test_path+filename_o+'_realC.jpg')
                save_images(C2B_imgs, [self.batch_size, 1],test_path+filename_o+'_C2B.jpg')
                save_images(C2B2C_imgs, [self.batch_size, 1],test_path+filename_o+'_C2B2C.jpg')
